import os
import re
import subprocess
import time

import utils

class BlockTraceManager(object):
    "This class provides interfaces to interact with blktrace"
    def __init__(self, dev, resultpath, to_ftlsim_path, sector_size):
        self.dev = dev
        self.resultpath = resultpath
        self.to_ftlsim_path = to_ftlsim_path
        self.sector_size = sector_size

    def start_tracing_and_collecting(self):
        self.proc = start_blktrace_on_bg(self.dev, self.resultpath)

    def stop_tracing_and_collecting(self):
        "this is not elegant... TODO:improve"
        stop_blktrace_on_bg()

    def blkparse_file_to_ftlsim_input_file(self):
        table = parse_blkparse_result(open(self.resultpath, 'r'))
        utils.prepare_dir_for_path(self.to_ftlsim_path)
        create_event_file(table, self.to_ftlsim_path,
            self.sector_size)

def start_blktrace_on_bg(dev, resultpath):
    utils.prepare_dir_for_path(resultpath)
    # cmd = "sudo blktrace -a write -a read -d {dev} -o - | blkparse -i - > "\
    # cmd = "sudo blktrace -a queue -d {dev} -o - | blkparse -a queue -i - > "\

    kernel_ver = utils.run_and_get_output('uname -r')[0].strip()
    if kernel_ver.startswith('4.1.5'):
        # trace_filter = 'complete'
        trace_filter = 'issue'
    elif kernel_ver.startswith('3.1.6'):
        trace_filter = 'queue'
    else:
        trace_filter = 'issue'
        print "WARNING: using blktrace filter {} for kernel {}".format(
            trace_filter, kernel_ver)
        time.sleep(5)

    cmd = "sudo blktrace -a {filtermask} -d {dev} -o - | "\
            "blkparse -a {filtermask} -i - >> "\
        "{resultpath}".format(dev = dev, resultpath = resultpath,
        filtermask = trace_filter)
    print cmd
    p = subprocess.Popen(cmd, shell=True)
    time.sleep(0.3) # wait to see if there's any immediate error.

    if p.poll() != None:
        raise RuntimeError("tracing failed to start")

    return p

def stop_blktrace_on_bg():
    utils.shcmd('pkill blkparse', ignore_error=True)
    utils.shcmd('pkill blktrace', ignore_error=True)
    utils.shcmd('sync')

    # try:
        # proc.terminate()
    # except Exception, e:
        # print e
        # exit(1)

def is_data_line(line):
    #                       devid    blockstart + nblocks
    match_obj = re.match( r'\d+,\d+.*\d+\s+\+\s+\d+', line)
    if match_obj == None:
        return False
    else:
        return True


def parse_blkparse_result(line_iter):
    def line2dic(line):
        """
        is_data_line() must be true for this line"\
        ['8,0', '0', '1', '0.000000000', '440', 'A', 'W', '12912077', '+', '8', '<-', '(8,2)', '606224']"
        """
        names = ['devid', 'cpuid', 'seqid', 'time', 'pid', 'action', 'RWBS', 'blockstart', 'ignore1', 'size']
        #        0        1         2       3        4      5         6       7             8          9
        items = line.split()

        dic = dict(zip(names, items))
        assert len(items) >= len(names)

        return dic

    table = []
    for line in line_iter:
        line = line.strip()
        # print is_data_line(line), line
        if is_data_line(line):
            ret = line2dic(line)
            ret['type'] = 'blkparse'
        else:
            ret = None

        if ret != None:
            table.append(ret)

    table.sort(key = lambda k: k['time'])
    return table

def create_event_file(table, out_path, sector_size):
    utils.prepare_dir_for_path(out_path)
    out = open(out_path, 'w')
    for row in table:
        if row['type'] == 'blkparse':
            pid = row['pid']
            blk_start = int(row['blockstart'])
            size = int(row['size'])

            byte_offset = blk_start * sector_size
            byte_size = size * sector_size

            if row['RWBS'] == 'D':
                operation = 'discard'
            elif 'W' in row['RWBS']:
                operation = 'write'
            elif 'R' in row['RWBS']:
                operation = 'read'
            else:
                raise RuntimeError('unknow operation')

            items = [str(x) for x in [pid, operation, byte_offset, byte_size]]
            line = ' '.join(items)+'\n'
        elif row['type'] == 'multiwriters':
            pid = 'NA'
            operation = 'finish'
            byte_offset = row['filepath']
            byte_size = 'NA'
            items = [str(x) for x in [pid, operation, byte_offset, byte_size]]
            line = ' '.join(items)+'\n'
        else:
            raise NotImplementedError()

        out.write( line )

    out.flush()
    os.fsync(out)
    out.close()





