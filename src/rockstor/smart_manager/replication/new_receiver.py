"""
Copyright (c) 2012-2013 RockStor, Inc. <http://rockstor.com>
This file is part of RockStor.

RockStor is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published
by the Free Software Foundation; either version 2 of the License,
or (at your option) any later version.

RockStor is distributed in the hope that it will be useful, but
WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <http://www.gnu.org/licenses/>.
"""

from multiprocessing import Process
import os
import sys
import zmq
import subprocess
from django.utils.timezone import utc
from django.conf import settings
from django import db
from contextlib import contextmanager
from util import ReplicationMixin
from fs.btrfs import (get_oldest_snap, remove_share, set_property, is_subvol)
from system.osi import run_command
from storageadmin.models import (Disk, Pool, Appliance)
from smart_manager.models import (ReplicaShare, ReceiveTrail)
import shutil
import time
from cli import APIWrapper
import json
import logging
logger = logging.getLogger(__name__)

BTRFS = '/sbin/btrfs'


class NewReceiver(ReplicationMixin, Process):

    def __init__(self, identity, meta):
        self.identity = identity
        self.meta = json.loads(meta)
        self.src_share = self.meta['share']
        self.dest_pool = self.meta['pool']
        self.incremental = self.meta['incremental']
        self.snap_name = self.meta['snap']
        self.sender_id = self.meta['uuid']
        self.sname = ('%s_%s' % (self.sender_id, self.src_share))
        self.snap_dir = ('%s%s/.snapshots/%s' % (settings.MNT_PT, self.dest_pool, self.sname))

        self.ppid = os.getpid()
        self.kb_received = 0
        self.rid = None
        self.rtid = None
        self.num_retain_snaps = 5
        self.ctx = zmq.Context()
        self.rp = None
        self.raw = None
        self.ack = False
        #close all db connections prior to fork.
        for alias, info in db.connections.databases.items():
            db.close_connection()
        super(NewReceiver, self).__init__()


    def _sys_exit(self, code):
        if (self.rp is not None and self.rp.returncode is None):
            try:
                self.rp.terminate()
            except Exception, e:
                logger.error('Exception while terminating the btrfs-recv process: %s' % e.__str__())
        self.ctx.destroy(linger=0)
        if (code == 0):
            logger.debug('Identity: %s. meta: %s Receive successful' % (self.identity, self.meta))
        sys.exit(code)

    @contextmanager
    def _clean_exit_handler(self):
        try:
            yield
        except Exception, e:
            logger.error('%s. Exception: %s' % (self.msg, e.__str__()))
            if (self.rtid is not None):
                try:
                    data = {'status': 'failed',
                            'error': self.msg, }
                    self.update_receive_trail(self.rtid, data)
                except Exception, e:
                    msg = ('Exception while updating receive trail for rtid(%d).' % self.rtid)
                    logger.error('%s. Exception: %s' % (msg, e.__str__()))

            if (self.ack is True):
                try:
                    command = 'receiver-error'
                    self.dealer.send_multipart(['receiver-error', b'%s. Exception: %s' %
                                                (str(self.msg), str(e.__str__()))])
                    #Retry logic here is overkill atm.
                    socks = dict(self.poll.poll(60000)) # 60 seconds
                    if (socks.get(self.dealer) == zmq.POLLIN):
                        msg = self.dealer.recv()
                        logger.debug('Response from the broker: %s' % msg)
                    else:
                        logger.debug('No response received from the broker for: %s. Aborting' % self.identity)
                except Exception, e:
                    msg = ('Exception while sending %s back to the broker from %s. Aborting' % (command, self.identity))
                    logger.error('%s. Exception: %s' % (msg, e.__str__()))
            self._sys_exit(3)

    def _delete_old_snaps(self, share_name, share_path, num_retain):
        oldest_snap = get_oldest_snap(share_path, num_retain, regex='_replication_')
        if (oldest_snap is not None):
            if (self.delete_snapshot(share_name, oldest_snap)):
                return self._delete_old_snaps(share_name, share_path, num_retain)

    def _send_recv(self, command, msg=''):
        rcommand = rmsg = None
        self.dealer.send_multipart([command, msg])
        #Retry logic doesn't make sense at. So one long patient wait.
        socks = dict(self.poll.poll(60000)) # 60 seconds.
        if (socks.get(self.dealer) == zmq.POLLIN):
            rcommand, rmsg = self.dealer.recv_multipart()
        logger.debug('Identity: %s command: %s rcommand: %s' %
                     (self.identity, command, rcommand))
        return rcommand, rmsg

    def _latest_snap(self, rso):
        for snap in ReceiveTrail.objects.filter(rshare=rso, status='succeeded').order_by('-id'):
            if (is_subvol('%s/%s' % (self.snap_dir, snap.snap_name))):
                return str(snap.snap_name) #cannot be unicode for zmq message
        logger.error('Id: %s. There are no replication snapshots on the system for '
                     'Share(%s).' % (self.identity, rso.share))
        #This would mean, a full backup transfer is required.
        return None

    def run(self):
        logger.debug('Starting a new receiver for meta: %s' % self.meta)
        self.msg = ('Top level exception in receiver')
        latest_snap = None
        with self._clean_exit_handler():
            self.law = APIWrapper()
            self.poll = zmq.Poller()
            self.dealer = self.ctx.socket(zmq.DEALER)
            self.dealer.setsockopt_string(zmq.IDENTITY, u'%s' % self.identity)
            self.dealer.set_hwm(10)
            self.dealer.connect('ipc:///tmp/foobar.ipc')
            self.poll.register(self.dealer, zmq.POLLIN)

            self.ack = True
            self.msg = ('Failed to get the sender ip for appliance: %s' % self.sender_id)
            self.sender_ip = Appliance.objects.get(uuid=self.sender_id).ip

            if (not self.incremental):
                self.msg = ('Failed to verify/create share: %s.' % self.sname)
                self.create_share(self.sname, self.dest_pool)

                self.msg = ('Failed to create the replica metadata object '
                            'for share: %s.' % self.sname)
                data = {'share': self.sname,
                        'appliance': self.sender_ip,
                        'src_share': self.src_share, }
                self.rid = self.create_rshare(data)
            else:
                self.msg = ('Failed to retreive the replica metadata object for '
                            'share: %s.' % self.sname)
                rso = ReplicaShare.objects.get(share=self.sname)
                self.rid = rso.id
                #Find and send the current snapshot to the sender. This will
                #be used as the start by btrfs-send diff.
                self.msg = ('Failed to verify latest replication snapshot on the system.')
                latest_snap = self._latest_snap(rso)

            self.msg = ('Failed to create receive trail for rid: %d' % self.rid)
            data = {'snap_name': self.snap_name, }
            self.rtid = self.create_receive_trail(self.rid, data)

            #delete the share, move the oldest snap to share
            self.msg = ('Failed to promote the oldest Snapshot to Share.')
            oldest_snap = get_oldest_snap(self.snap_dir, self.num_retain_snaps, regex='_replication_')
            if (oldest_snap is not None):
                snap_path = ('%s/%s' % (self.snap_dir, oldest_snap))
                share_path = ('%s%s/%s' %
                              (settings.MNT_PT, self.dest_pool,
                               self.sname))
                pool = Pool.objects.get(name=self.dest_pool)
                remove_share(pool, self.sname, '-1/-1')
                set_property(snap_path, 'ro', 'false',
                             mount=False)
                run_command(['/usr/bin/rm', '-rf', share_path],
                            throw=False)
                shutil.move(snap_path, share_path)
                self.delete_snapshot(self.sname, oldest_snap)

            self.msg = ('Failed to prune old Snapshots')
            self._delete_old_snaps(self.sname, self.snap_dir, self.num_retain_snaps + 1)

            self.msg = ('Failed to validate the source share(%s) on sender(uuid: %s '
                        ') Did the ip of the sender change?' %
                        (self.src_share, self.sender_id))
            self.validate_src_share(self.sender_id, self.src_share)

            sub_vol = ('%s%s/%s' % (settings.MNT_PT, self.dest_pool, self.sname))
            if (not is_subvol(sub_vol)):
                self.msg = ('Failed to create parent subvolume %s' % sub_vol)
                run_command([BTRFS, 'subvolume', 'create', sub_vol])

            self.msg = ('Failed to create snapshot directory: %s' % self.snap_dir)
            run_command(['/usr/bin/mkdir', '-p', self.snap_dir])
            snap_fp = ('%s/%s' % (self.snap_dir, self.snap_name))

            #If the snapshot already exists, presumably from the previous attempt and
            #the sender tries to send the same, reply back with snap_exists and do not
            #start the btrfs-receive
            if (is_subvol(snap_fp)):
                logger.debug('Id: %s. Snapshot to be sent(%s) already exists. Not '
                             'starting a new receive process' % (self.identity, snap_fp))
                self._send_recv('snap-exists')
                self._sys_exit(0)

            cmd = [BTRFS, 'receive', self.snap_dir]
            self.msg = ('Failed to start the low level btrfs receive command(%s)'
                        '. Aborting.' % cmd)
            self.rp = subprocess.Popen(cmd, shell=False, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)

            self.msg = ('Failed to send receiver-ready for identity: %s' % self.identity)
            rcommand, rmsg = self._send_recv('receiver-ready', latest_snap or '')
            if (rcommand is None):
                self._sys_exit(3)

            term_commands = ('btrfs-send-init-error', 'btrfs-send-unexpected-termination-error',
                             'btrfs-send-nonzero-termination-error',)
            num_tries = 10
            poll_interval = 6000 # 6 seconds
            while (True):
                socks = dict(self.poll.poll(poll_interval))
                if (socks.get(self.dealer) == zmq.POLLIN):
                    #reset to wait upto 60(poll_interval x num_tries milliseconds) for every message
                    num_tries = 10
                    command, message = self.dealer.recv_multipart()
                    if (command == 'btrfs-send-stream-finished'):
                        #this command concludes fsdata transfer. After this, btrfs-recev
                        #process should be terminated(.communicate).
                        if (self.rp.poll() is None):
                            self.msg = ('Failed to terminate btrfs-recv command')
                            out, err = self.rp.communicate()
                            out = out.split('\n')
                            err = err.split('\n')
                            logger.debug('Id: %s. Terminated btrfs-recv. cmd = %s '
                                         'out = %s err: %s rc: %s' %
                                         (self.identity, cmd, out, err, self.rp.returncode))
                        if (self.rp.returncode != 0):
                            self.msg = ('btrfs-recv exited with unexpected exitcode(%s). ' % self.rp.returncode)
                            raise Exception(self.msg)
                        self._send_recv('btrfs-recv-finished')
                        self.refresh_share_state()
                        self.refresh_snapshot_state()

                        self.msg = ('Failed to update receive trail for rtid: %d' % self.rtid)
                        self.update_receive_trail(self.rtid, {'status': 'succeeded',})
                        self._sys_exit(0)

                    if (command in term_commands):
                        self.msg = ('Terminal command(%s) received from the sender. Aborting.' % command)
                        raise Exception(self.msg)

                    if (self.rp.poll() is None):
                        self.rp.stdin.write(message)
                        self.rp.stdin.flush()
                        #@todo: implement advanced credit request system.
                        self.dealer.send_multipart([b'send-more', ''])
                    else:
                        out, err = self.rp.communicate()
                        out = out.split('\n')
                        err = err.split('\n')
                        logger.error('Id: %s. btrfs-recv died unexpectedly. cmd: %s out: %s. err: %s' %
                                     (self.identity, cmd, out, err))
                        msg = ('Low level system error from btrfs receive '
                               'command. cmd: %s out: %s err: %s for rtid: %s'
                               % (cmd, out, err, self.rtid))
                        data = {'status': 'failed',
                                'error': msg, }
                        self.msg = ('Failed to update receive trail for rtid: %d.' % self.rtid)
                        self.update_receive_trail(self.rtid, data)
                        self.msg = msg
                        raise Exception(self.msg)
                else:
                    num_tries -= 1
                    msg = ('Id: %s. No response received from the broker. '
                           'remaining tries: %d' % (self.identity, num_tries))
                    logger.error(msg)
                    if (num_tries == 0):
                        self.msg = ('%s. Terminating the receiver.' % msg)
                        raise Exception(self.msg)
