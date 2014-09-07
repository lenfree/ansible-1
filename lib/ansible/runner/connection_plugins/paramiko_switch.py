# (c) 2012, Michael DeHaan <michael.dehaan@gmail.com>
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.


# ---
# The paramiko transport is provided because many distributions, in particular EL6 and before
# do not support ControlPersist in their SSH implementations.  This is needed on the Ansible
# control machine to be reasonably efficient with connections.  Thus paramiko is faster
# for most users on these platforms.  Users with ControlPersist capability can consider
# using -c ssh or configuring the transport in ansible.cfg.

import warnings
import os
import pipes
import socket
import random
import logging
import tempfile
import traceback
import fcntl
import re
import sys
from termios import tcflush, TCIFLUSH
from binascii import hexlify
from ansible.callbacks import vvv
from ansible import errors
from ansible import utils
from ansible import constants as C
            
AUTHENTICITY_MSG="""
paramiko: The authenticity of host '%s' can't be established. 
The %s key fingerprint is %s. 
Are you sure you want to continue connecting (yes/no)?
"""

# prevent paramiko warning noise -- see http://stackoverflow.com/questions/3920502/
HAVE_PARAMIKO=False
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    try:
        import paramiko
        HAVE_PARAMIKO=True
        logging.getLogger("paramiko").setLevel(logging.WARNING)
    except ImportError:
        pass

class MyAddPolicy(object):
    """
    Based on AutoAddPolicy in paramiko so we can determine when keys are added
    and also prompt for input.

    Policy for automatically adding the hostname and new host key to the
    local L{HostKeys} object, and saving it.  This is used by L{SSHClient}.
    """

    def __init__(self, runner): 
        self.runner = runner

    def missing_host_key(self, client, hostname, key):

        if C.HOST_KEY_CHECKING:

            fcntl.lockf(self.runner.process_lockfile, fcntl.LOCK_EX)
            fcntl.lockf(self.runner.output_lockfile, fcntl.LOCK_EX)

            old_stdin = sys.stdin
            sys.stdin = self.runner._new_stdin
            fingerprint = hexlify(key.get_fingerprint())
            ktype = key.get_name()
            
            # clear out any premature input on sys.stdin
            tcflush(sys.stdin, TCIFLUSH)

            inp = raw_input(AUTHENTICITY_MSG % (hostname, ktype, fingerprint))
            sys.stdin = old_stdin
            if inp not in ['yes','y','']:
                fcntl.flock(self.runner.output_lockfile, fcntl.LOCK_UN)
                fcntl.flock(self.runner.process_lockfile, fcntl.LOCK_UN)
                raise errors.AnsibleError("host connection rejected by user")

            fcntl.lockf(self.runner.output_lockfile, fcntl.LOCK_UN)
            fcntl.lockf(self.runner.process_lockfile, fcntl.LOCK_UN)


        key._added_by_ansible_this_time = True

        # existing implementation below:
        client._host_keys.add(hostname, key.get_name(), key)

        # host keys are actually saved in close() function below
        # in order to control ordering.
        

# keep connection objects on a per host basis to avoid repeated attempts to reconnect

SSH_CONNECTION_CACHE = {}
SFTP_CONNECTION_CACHE = {}

class Connection(object):
    ''' SSH based connections with Paramiko '''

    def __init__(self, runner, host, port, user, password, private_key_file, *args, **kwargs):

        self.ssh = None
        self.sftp = None
        self.runner = runner
        self.host = host
        self.port = port or 22
        self.user = user
        self.password = password
        self.private_key_file = private_key_file
        self.has_pipelining = False

    def _cache_key(self):
        return "%s__%s__" % (self.host, self.user)

    def connect(self):
        cache_key = self._cache_key()
        if cache_key in SSH_CONNECTION_CACHE:
            self.ssh = SSH_CONNECTION_CACHE[cache_key]
        else:
            self.ssh = SSH_CONNECTION_CACHE[cache_key] = self._connect_uncached()
        return self

    def _connect_uncached(self):
        ''' activates the connection object '''

        if not HAVE_PARAMIKO:
            raise errors.AnsibleError("paramiko is not installed")

        vvv("ESTABLISH CONNECTION FOR USER: %s on PORT %s TO %s" % (self.user, self.port, self.host), host=self.host)

        ssh = paramiko.SSHClient()

        self.keyfile = os.path.expanduser("~/.ssh/known_hosts")

        if C.HOST_KEY_CHECKING:
            ssh.load_system_host_keys()

        #ssh.set_missing_host_key_policy(MyAddPolicy(self.runner))
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        allow_agent = False

        try:

            if self.private_key_file:
                key_filename = os.path.expanduser(self.private_key_file)
            elif self.runner.private_key_file:
                key_filename = os.path.expanduser(self.runner.private_key_file)
            else:
                key_filename = None

            # connect to host
            ssh.connect(self.host,
                        username=self.user,
                        allow_agent=allow_agent,
                        look_for_keys=False,
                        key_filename=key_filename,
                        password=self.password,
                        timeout=self.runner.timeout,
                        port=self.port)

        except Exception, e:

            msg = str(e)
            if "PID check failed" in msg:
                raise errors.AnsibleError("paramiko version issue, please upgrade paramiko on the machine running ansible")
            elif "Private key file is encrypted" in msg:
                msg = 'ssh %s@%s:%s : %s\nTo connect as a different user, use -u <username>.' % (
                    self.user, self.host, self.port, msg)
                raise errors.AnsibleConnectionFailed(msg)
            else:
                raise errors.AnsibleConnectionFailed(msg)

        return ssh

    def exec_command(self,
                     cmd,
                     tmp_path,
                     sudo_user=None,
                     sudoable=False,
                     executable=None,
                     in_data=None,
                     su=None,
                     su_user=None):
        ''' run a command on the remote host '''

        bufsize = 4096

        try:
            chan = self.ssh.invoke_shell()
        except Exception, e:
            msg = "Failed to open session"
            if len(str(e)) > 0:
                msg += ": %s" % str(e)
            raise errors.AnsibleConnectionFailed(msg)

        cmd += "\n"
        chan.send(cmd)

    def _any_keys_added(self):

        added_any = False
        for hostname, keys in self.ssh._host_keys.iteritems():
            for keytype, key in keys.iteritems():
                added_this_time = getattr(key, '_added_by_ansible_this_time', False)
                if added_this_time:
                    return True
        return False

    def _save_ssh_host_keys(self, filename):
        ''' 
        not using the paramiko save_ssh_host_keys function as we want to add new SSH keys at the bottom so folks 
        don't complain about it :) 
        '''

        if not self._any_keys_added():
            return False

        path = os.path.expanduser("~/.ssh")
        if not os.path.exists(path):
            os.makedirs(path)

        f = open(filename, 'w')

        for hostname, keys in self.ssh._host_keys.iteritems():

            for keytype, key in keys.iteritems():

                # was f.write
                added_this_time = getattr(key, '_added_by_ansible_this_time', False)
                if not added_this_time:
                    f.write("%s %s %s\n" % (hostname, keytype, key.get_base64()))

        for hostname, keys in self.ssh._host_keys.iteritems():

            for keytype, key in keys.iteritems():
                added_this_time = getattr(key, '_added_by_ansible_this_time', False)
                if added_this_time:
                    f.write("%s %s %s\n" % (hostname, keytype, key.get_base64()))

        f.close()

    def close(self):
        ''' terminate the connection '''

        cache_key = self._cache_key()
        SSH_CONNECTION_CACHE.pop(cache_key, None)
        SFTP_CONNECTION_CACHE.pop(cache_key, None)

        if C.HOST_KEY_CHECKING and C.PARAMIKO_RECORD_HOST_KEYS and self._any_keys_added():

            # add any new SSH host keys -- warning -- this could be slow
            lockfile = self.keyfile.replace("known_hosts",".known_hosts.lock") 
            dirname = os.path.dirname(self.keyfile)
            if not os.path.exists(dirname):
                os.makedirs(dirname)

            KEY_LOCK = open(lockfile, 'w')
            fcntl.lockf(KEY_LOCK, fcntl.LOCK_EX)

            try:
                # just in case any were added recently

                self.ssh.load_system_host_keys()
                self.ssh._host_keys.update(self.ssh._system_host_keys)

                # gather information about the current key file, so
                # we can ensure the new file has the correct mode/owner

                key_dir  = os.path.dirname(self.keyfile)
                key_stat = os.stat(self.keyfile)

                # Save the new keys to a temporary file and move it into place
                # rather than rewriting the file. We set delete=False because
                # the file will be moved into place rather than cleaned up.

                tmp_keyfile = tempfile.NamedTemporaryFile(dir=key_dir, delete=False)
                os.chmod(tmp_keyfile.name, key_stat.st_mode & 07777)
                os.chown(tmp_keyfile.name, key_stat.st_uid, key_stat.st_gid)

                self._save_ssh_host_keys(tmp_keyfile.name)
                tmp_keyfile.close()

                os.rename(tmp_keyfile.name, self.keyfile)

            except:

                # unable to save keys, including scenario when key was invalid
                # and caught earlier
                traceback.print_exc()
                pass
            fcntl.lockf(KEY_LOCK, fcntl.LOCK_UN)

        self.ssh.close()
