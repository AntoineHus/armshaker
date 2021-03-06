#!/usr/bin/env python3
import subprocess
import time
import multiprocessing
import curses
import atexit
import sys
import argparse
import fcntl
import time
import math

WORKER_AREA_WIDTH = 45

def get_status(proc_num):
    f = open('data/status{}'.format(proc_num), 'r')
    fcntl.flock(f, fcntl.LOCK_EX)

    lines = f.readlines()

    fcntl.flock(f, fcntl.LOCK_UN)
    f.close()

    status = {}
    for line in lines:
        try:
            key, val = line.split(':', maxsplit=1)
        except ValueError:
            if line == '\n':
                # Skip empty lines
                continue
            print("ERROR: Ill-formatted statusfile")
            return None
        status[key] = val.replace('\t', ' ').strip()

    # TODO: Remove nasty hardcode
    if len(status) != 8:
        # Sometimes we read the statusfile while it's being written to.
        # Ideally we should have a lock or something, but this works for now...
        return None

    return status


def update_statuses(procs, statuses):
    # Read the statusfiles
    for proc_num in range(len(procs)):
        status = get_status(proc_num)
        if status is not None:
            statuses[proc_num] = status
    return statuses


def print_worker(pad, proc_num, status, global_y_offset):
    lines = []
    lines.append('insn:      {}'.format(status['insn']))
    lines.append('cs_disas:  {}'.format(status['cs_disas']))
    lines.append('opc_disas: {}'.format(status['libopcodes_disas']))
    lines.append('checked:   {:,}'.format(int(status['instructions_checked'])))
    lines.append('skipped:   {:,}'.format(int(status['instructions_skipped'])))
    lines.append('filtered:  {:,}'.format(int(status['instructions_filtered'])))
    lines.append('hidden:    {:,}'.format(int(status['hidden_instructions_found'])))
    lines.append('ips:       {:,}'.format(int(status['instructions_per_sec'])))

    max_line_length = WORKER_AREA_WIDTH - 4
    for line_num in range(len(lines)):
        lines[line_num] = lines[line_num][:max_line_length].ljust(max_line_length)

    y_offset = ((2+len(lines))*(proc_num // 2)) + global_y_offset
    x_offset = (proc_num % 2)*(WORKER_AREA_WIDTH+2) + 1

    header = '╔═ Worker {} '.format(proc_num).ljust(WORKER_AREA_WIDTH-1, '═') + '╗'
    pad.addstr(y_offset, x_offset, header)
    for line_num in range(len(lines)):
        pad.addstr(y_offset+1+line_num, x_offset, '║ {} ║'.format(lines[line_num]))
    footer = '╚'.ljust(WORKER_AREA_WIDTH-1, '═') + '╝'
    pad.addstr(y_offset+1+len(lines), x_offset, footer)


def print_summary(pad, statuses, extra_data, just_height=False):
    sum_status = {
            'checked': 0,
            'skipped': 0,
            'filtered': 0,
            'hidden': 0,
            'ips': 0,
            'insns_so_far': 0
    }

    for status in statuses:
        if status is None:
            continue
        sum_status['checked'] += int(status['instructions_checked'])
        sum_status['skipped'] += int(status['instructions_skipped'])
        sum_status['filtered'] += int(status['instructions_filtered'])
        sum_status['hidden'] += int(status['hidden_instructions_found'])
        sum_status['ips'] += int(status['instructions_per_sec'])

        sum_status['insns_so_far'] += (int(status['instructions_checked'])
                                     + int(status['instructions_skipped'])
                                     + int(status['instructions_filtered']))

    total_insns = extra_data['search_range'][1] - extra_data['search_range'][0] + 1
    progress = (sum_status['insns_so_far'] / total_insns) * 100
    elapsed_hrs = (time.time() - extra_data['time_started']) / 60 / 60

    if sum_status['ips'] != 0:
        eta_hrs = ((total_insns - sum_status['insns_so_far']) / sum_status['ips']) / 60 / 60
    else:
        eta_hrs = float('inf')

    lines = []
    lines.append('checked:   {:,}'.format(int(sum_status['checked'])))
    lines.append('skipped:   {:,}'.format(int(sum_status['skipped'])))
    lines.append('filtered:  {:,}'.format(int(sum_status['filtered'])))
    lines.append('hidden:    {:,}'.format(int(sum_status['hidden'])))
    lines.append('ips:       {:,}'.format(int(sum_status['ips'])))
    lines.append('progress:  {:.3f}%'.format(progress))
    lines.append('elapsed:   {:.2f}hrs'.format(elapsed_hrs))
    lines.append('eta:       {:.1f}hrs'.format(eta_hrs))

    max_line_length = (WORKER_AREA_WIDTH) + 2
    max_height = math.ceil(len(lines) / 2)

    for line_num in range(len(lines)):
        lines[line_num] = lines[line_num][:max_line_length].ljust(max_line_length)

    y_offset = 1
    x_offset = 1

    if not just_height:
        header = '╔═ Summary '.ljust(max_line_length*2-3, '═') + '╗'
        pad.addstr(y_offset, x_offset, header)
        # Add actual strings
        for line_num in range(len(lines)):
            pad.addstr(y_offset+1+(line_num % max_height),
                          x_offset + (line_num // max_height)*max_line_length,
                          '  {}  '.format(lines[line_num]))
        # Add border
        for line_num in range(max_height):
            pad.addstr(y_offset+1+line_num, x_offset, '║')
            pad.addstr(y_offset+1+line_num, x_offset+max_line_length*2-3, '║')
        footer = '╚'.ljust(max_line_length*2-3, '═') + '╝'
        pad.addstr(y_offset+1+max_height, x_offset, footer)

    return max_height + 3


def print_done(pad):
    y_offset = 17
    x_offset = WORKER_AREA_WIDTH - 8
    pad.addstr(y_offset+0, x_offset, '╔═══════════════════╗')
    pad.addstr(y_offset+1, x_offset, '║                   ║')
    pad.addstr(y_offset+2, x_offset, '║       Done!       ║')
    pad.addstr(y_offset+3, x_offset, '║ (press q to quit) ║')
    pad.addstr(y_offset+4, x_offset, '║                   ║')
    pad.addstr(y_offset+5, x_offset, '╚═══════════════════╝')


def update_screen(pad, statuses, extra_data):
    # Sometimes reading the status files fails. In those cases, don't
    # update the values, as they will be incorrect
    height = print_summary(pad, statuses, extra_data, None in statuses)

    # Print workers
    for proc_num, status in enumerate(statuses):
        if status is None:
            continue
        print_worker(pad, proc_num, status, height+1)


def start_procs(search_range, args):
    procs = []
    if (type(args.workers) == int  # Default val
            or args.workers[0] <= 0):
        proc_count = multiprocessing.cpu_count()
    else:
        proc_count = args.workers[0]
    proc_search_size = int((search_range[1] - search_range[0] + 1) / proc_count)
    for i in range(proc_count):
        insn_start = search_range[0] + proc_search_size * i
        insn_end  = search_range[0] + (proc_search_size) * (i + 1) - 1
        if i == proc_count - 1:
            insn_end = search_range[1]

        cmd = ['./fuzzer',
               '-l', str(i),
               '-s', hex(insn_start),
               '-e', hex(insn_end),
               '-d' if args.discreps else '',
               '-p' if args.ptrace else '',
               '-n' if args.no_exec else '',
               '-f{}'.format(args.filter[0]) if args.filter and args.filter[0] > 0 else '',
               '-t' if args.thumb else '',
               '-z' if args.random else '',
               '-g' if args.log_reg_changes else '',
               '-V' if args.vector else '',
               '-c' if args.cond else '',
               '-q']

        try:
            proc = subprocess.Popen(cmd,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                    stdin=subprocess.PIPE)
        except FileNotFoundError:
            return 0
        procs.append(proc)

    return procs


def exit_handler(procs):
    for proc in procs:
        proc.kill()


def refresh_pad(stdscr, pad):
    y_size, x_size = stdscr.getmaxyx()
    pad.refresh(0, 0, 0, 0, y_size-1, x_size-1)


def main(stdscr, args):
    search_range = (args.start if type(args.start) is int else args.start[0],
                    args.end if type(args.end) is int else args.end[0])
    procs = start_procs(search_range, args)

    if procs == 0:
        return 'The fuzzer binary was not found. It likely needs to be ' \
               'compiled with "make" first.'

    curses.use_default_colors()
    curses.cbreak()
    curses.noecho()
    curses.curs_set(False)
    stdscr.keypad(True)
    stdscr.nodelay(True)

    pad = curses.newpad(100, 100)
    pad.nodelay(True)
    pad.keypad(True)

    atexit.register(exit_handler, procs)

    extra_data = {
            'search_range': search_range,
            'time_started': time.time()
    }

    quit_str = 'Done'
    statuses = [None] * len(procs)

    while True:
        try:
            update_statuses(procs, statuses)
            update_screen(pad, statuses, extra_data)
            refresh_pad(stdscr, pad)
            if stdscr.getch() == ord('q'):
                quit_str = 'User abort'
                break
            quit = False
            done = True
            for i in range(len(procs)):
                ret = procs[i].poll()
                if ret == 1:
                    outs, errs = procs[i].communicate()
                    quit_str = 'Worker {} crashed:\n{}'.format(i, errs.decode('utf-8'))
                    quit = True
                    break
                elif ret != 0:
                    done = False
                    break
            if quit:
                break
            elif done:
                # All processes terminated sucessfully.
                # When done, update one last time, show a message and
                # wait for any key before quitting
                stdscr.nodelay(False)
                pad.nodelay(False)
                update_statuses(procs, statuses)
                update_screen(pad, statuses, extra_data)
                print_done(pad)
                refresh_pad(stdscr, pad)
                while stdscr.getch() != ord('q'):
                    pass
                break
            else:
                time.sleep(0.1)
        except FileNotFoundError:
            # Wait a little if the status files haven't been created yet
            time.sleep(0.1)
        except KeyboardInterrupt:
            quit_str = 'User abort'
            break

    curses.nocbreak()
    stdscr.keypad(False)
    pad.keypad(False)
    curses.echo()
    curses.curs_set(True)
    curses.endwin()

    return quit_str


if __name__ == '__main__':
    def hex_int(x):
        return int(x, 16)

    parser = argparse.ArgumentParser(description='fuzzer front-end')
    parser.add_argument('-s', '--start',
                        type=hex_int, nargs=1,
                        help='search range start',
                        metavar='INSN', default=0)
    parser.add_argument('-e', '--end',
                        type=hex_int, nargs=1,
                        help='search range end',
                        metavar='INSN', default=0xffffffff)
    parser.add_argument('-d', '--discreps',
                        action='store_true',
                        help='Log disassembler discrepancies')
    parser.add_argument('-w', '--workers',
                        type=int, nargs=1,
                        help='Number of worker processes',
                        metavar='NUM', default=0)
    parser.add_argument('-p', '--ptrace',
                        action='store_true',
                        help='Use ptrace when testing')
    parser.add_argument('-n', '--no-exec',
                        action='store_true',
                        help='Don\'t execute instructions, just disassemble them.')
    parser.add_argument('-f', '--filter',
                        type=int, nargs=1,
                        help='Filter certain instructions',
                        metavar='LEVEL', default=0)
    parser.add_argument('-t', '--thumb',
                        action='store_true',
                        help='Use the thumb instruction set (only on AArch32).')
    parser.add_argument('-z', '--random',
                        action='store_true',
                        help='Load the registers with random values, instead of all 0s.')
    parser.add_argument('-g', '--log-reg-changes',
                        action='store_true',
                        help='For hidden instructions, only log registers that changed value.')
    parser.add_argument('-V', '--vector',
                        action='store_true',
                        help='Set and log vector registers (d0-d31, fpscr) when fuzzing.')
    parser.add_argument('-c', '--cond',
                        action='store_true',
                        help='Set cpsr flags to match instruction condition code.')

    args = parser.parse_args()
    quit_str = curses.wrapper(main, args)
    print(quit_str)
