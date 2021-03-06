#!/usr/bin/python

"""OpenTuner plays Super Mario Bros. for NES

We write a movie file and ask the emulator to play it back while running
fceux-hook.lua, which checks for death/flagpole and prints the fitness to
stdout where OpenTuner, as the parent process, can read it.
"""

import adddeps #fix sys.path
import argparse
import base64
import pickle
import tempfile
import subprocess
import re
import zlib
import abc
import sys
import os

import opentuner
from opentuner.search.manipulator import ConfigurationManipulator, IntegerParameter, EnumParameter, BooleanParameter
from opentuner.measurement import MeasurementInterface
from opentuner.measurement.inputmanager import FixedInputManager
from opentuner.tuningrunmain import TuningRunMain
from opentuner.search.objective import MinimizeTime

class InstantiateAction(argparse.Action):
  def __init__(self, *pargs, **kwargs):
    super(InstantiateAction, self).__init__(*pargs, **kwargs)

  def __call__(self, parser, namespace, values, option_string=None):
    setattr(namespace, self.dest, getattr(sys.modules[__name__], values)())

argparser = argparse.ArgumentParser(parents=opentuner.argparsers())
argparser.add_argument('--tuning-run', help='concatenate new bests from given tuning run into single movie')
argparser.add_argument('--headful', action='store_true', help='run headful (not headless) for debugging or live demo')
argparser.add_argument('--xvfb-delay', type=int, default=0, help='delay between launching xvfb and fceux')
argparser.add_argument('--fceux-path', default='fceux', help='path to fceux executable')
argparser.add_argument('--representation', default='DurationRepresentation', action=InstantiateAction, help='name of representation class')
argparser.add_argument('--fitness-function', default='Progress', action=InstantiateAction, help='name of fitness function class')

# Functions for building FCEUX movie files (.fm2 files)

def fm2_line(up, down, left, right, a, b, start, select, reset=False):
  """formats one frame of input with the given button presses"""
  return ''.join(('|1|' if reset else '|0|') +
    ('R' if right else '.') +
    ('L' if left else '.') +
    ('D' if down else '.') +
    ('U' if up else '.') +
    ('T' if start else '.') +
    ('D' if select else '.') +
    ('B' if b else '.') +
    ('A' if a else '.') +
    '|........||')

def maxd(iterable, default):
  try:
    return max(iterable)
  except ValueError:
    return default

def fm2_lines(up, down, left, right, a, b, start, select, reset=set(), minFrame=None, maxFrame=None):
  """formats many frames using the given button-press sets"""
  if minFrame is None:
    minFrame = 0
  if maxFrame is None:
    maxFrame = max(maxd(up, 0), maxd(down, 0), maxd(left, 0), maxd(right, 0), maxd(a, 0), maxd(b, 0), maxd(start, 0), maxd(select, 0), maxd(reset, 0)) + 1
  lines = list()
  for i in range(minFrame, maxFrame):
    lines.append(fm2_line(i in up, i in down, i in left, i in right, i in a, i in b, i in start, i in select, i in reset))
  return lines

def fm2_smb_header():
  return ["version 3",
    "emuVersion 9828",
    "romFilename smb.nes",
    "romChecksum base64:jjYwGG411HcjG/j9UOVM3Q==",
    "guid 51473540-E9D7-11E3-ADFC-46CE3219C4E0",
    "fourscore 0",
    "port0 1",
    "port1 1",
    "port2 0"]

def fm2_smb(left, right, down, b, a, header=True, padding=True, minFrame=None, maxFrame=None):
  reset = set()
  start = set()
  if padding:
    left = set([x+196 for x in left])
    right = set([x+196 for x in right])
    down = set([x+196 for x in down])
    b = set([x+196 for x in b])
    a = set([x+196 for x in a])
    reset.add(0)
    start.add(33)
  lines = fm2_lines(set(), down, left, right, a, b, start, set(), reset, minFrame, maxFrame)
  if header:
    return "\n".join(fm2_smb_header() + lines)
  else:
    return "\n".join(lines)

def run_movie(fm2, args):
  with tempfile.NamedTemporaryFile(suffix=".fm2", delete=True) as f:
    f.write(fm2)
    f.flush()
    cmd = []
    if not args.headful:
      cmd += ["xvfb-run", "-a", "-w", str(args.xvfb_delay)]
    cmd += [args.fceux_path, "--playmov", f.name, "--loadlua",
        "fceux-hook.lua", "--nogui", "--volume", "0", "--no-config", "1",
        "smb.nes"]
    stdout, stderr = subprocess.Popen(cmd, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE).communicate()
  match = re.search(r"^(won|died) (\d+) (\d+)$", stdout, re.MULTILINE)
  if not match:
    print(stderr)
    print(stdout)
    raise ValueError
  wl = match.group(1)
  x_pos = int(match.group(2))
  framecount = int(match.group(3))
  return (wl, x_pos, framecount)

class Representation(object, metaclass=abc.ABCMeta):
  """Interface for pluggable tuning representations."""

  @abc.abstractmethod
  def manipulator():
    """Return a ConfigurationManipulator for this representation."""
    pass

  @abc.abstractmethod
  def interpret(cfg):
    """Unpack this representation into button-press sets (L, R, D, B, A)."""
    pass

class NaiveRepresentation(Representation):
  """Uses a parameter per (button, frame) pair."""
  def manipulator(self):
    m = ConfigurationManipulator()
    for i in range(0, 12000):
      m.add_parameter(BooleanParameter('L{}'.format(i)))
      m.add_parameter(BooleanParameter('R{}'.format(i)))
      m.add_parameter(BooleanParameter('D{}'.format(i)))
      m.add_parameter(BooleanParameter('B{}'.format(i)))
      m.add_parameter(BooleanParameter('A{}'.format(i)))
    return m

  def interpret(self, cfg):
    left = set()
    right = set()
    down = set()
    running = set()
    jumping = set()
    for i in range(0, 12000):
      if cfg['L{}'.format(i)]:
        left.add(i)
      if cfg['R{}'.format(i)]:
        right.add(i)
      if cfg['D{}'.format(i)]:
        down.add(i)
      if cfg['B{}'.format(i)]:
        running.add(i)
      if cfg['A{}'.format(i)]:
        jumping.add(i)
    return left, right, down, running, jumping

class DurationRepresentation(Representation):
  def manipulator(self):
    m = ConfigurationManipulator()
    for i in range(0, 1000):
      #bias 3:1 in favor of moving right
      m.add_parameter(EnumParameter('move{}'.format(i), ["R", "L", "RB", "LB", "N", "LR", "LRB", "R2", "RB2", "R3", "RB3"]))
      m.add_parameter(IntegerParameter('move_duration{}'.format(i), 1, 60))
      #m.add_parameter(BooleanParameter("D"+str(i)))
    for i in range(0, 1000):
      m.add_parameter(IntegerParameter('jump_frame{}'.format(i), 0, 24000))
      m.add_parameter(IntegerParameter('jump_duration{}'.format(i), 1, 32))
    return m

  def interpret(self, cfg):
    left = set()
    right = set()
    down = set()
    running = set()
    start = 0
    for i in range(0, 1000):
      move = cfg['move{}'.format(i)]
      move_duration = cfg['move_duration{}'.format(i)]
      if "R" in move:
        right.update(range(start, start + move_duration))
      if "L" in move:
        left.update(range(start, start + move_duration))
      if "B" in move:
        running.update(range(start, start + move_duration))
      start += move_duration
    jumping = set()
    for i in range(0, 1000):
      jump_frame = cfg['jump_frame{}'.format(i)]
      jump_duration = cfg['jump_duration{}'.format(i)]
      jumping.update(range(jump_frame, jump_frame + jump_duration))
    return left, right, down, running, jumping

class AlphabetRepresentation(Representation):
  def manipulator(self):
    m = ConfigurationManipulator()
    for i in range(0, 400*60):
      m.add_parameter(EnumParameter('{}'.format(i), range(0, 16)))
    return m

  def interpret(self, cfg):
    left = set()
    right = set()
    down = set()
    running = set()
    jumping = set()
    for i in range(0, 400*60):
      bits = cfg[str(i)]
      if bits & 1:
        left.add(i)
      if bits & 2:
        right.add(i)
      if bits & 4:
        running.add(i)
      if bits & 8:
        jumping.add(i)
      #if bits & 16:
      #  down.add(i)
    return left, right, down, running, jumping

class FitnessFunction(object, metaclass=abc.ABCMeta):
  """Interface for pluggable fitness functions."""

  @abc.abstractmethod
  def __call__(won, x_pos, elapsed_frames):
    """Return the fitness (float, lower is better)."""
    pass

class Progress(FitnessFunction):
  def __call__(self, won, x_pos, elapsed_frames):
    return -float(x_pos)

class ProgressPlusTimeRemaining(FitnessFunction):
  def __call__(self, won, x_pos, elapsed_frames):
    """x_pos plus 1 for each frame remaining on the timer on a win.  This results in a large discontinuity at wins.  This was the fitness function used for the OpenTuner paper, though the paper only discussed time-to-first-win."""
    return -float(x_pos + 400*60 - elapsed_frames) if won else -float(x_pos)

class ProgressTimesAverageSpeed(FitnessFunction):
  def __call__(self, won, x_pos, elapsed_frames):
    return -x_pos * (float(x_pos)/elapsed_frames)

class SMBMI(MeasurementInterface):
  def __init__(self, args):
    super(SMBMI, self).__init__(args)
    self.parallel_compile = True
    self.args = args

  def manipulator(self):
    return self.args.representation.manipulator()

  def compile(self, cfg, id):
    left, right, down, running, jumping = self.args.representation.interpret(cfg)
    fm2 = fm2_smb(left, right, down, running, jumping)
    try:
      wl, x_pos, framecount = run_movie(fm2, self.args)
    except ValueError:
      return opentuner.resultsdb.models.Result(state='ERROR', time=float('inf'))
    print(wl, x_pos, framecount)
    return opentuner.resultsdb.models.Result(state='OK', time=self.args.fitness_function("won" in wl, x_pos, framecount))

  def run_precompiled(self, desired_result, input, limit, compile_result, id):
    return compile_result

  def run(self, desired_result, input, limit):
    pass

def new_bests_movie(args):
  (stdout, stderr) = subprocess.Popen(["sqlite3", args.database, "select configuration_id from result where tuning_run_id = %d and was_new_best = 1 order by collection_date;" % args.tuning_run], stdout=subprocess.PIPE, stderr=subprocess.PIPE).communicate()
  cids = stdout.split()
  print('\n'.join(fm2_smb_header()))
  for cid in cids:
    (stdout, stderr) = subprocess.Popen(["sqlite3", args.database, "select quote(data) from configuration where id = %d;" % int(cid)], stdout=subprocess.PIPE, stderr=subprocess.PIPE).communicate()
    cfg = pickle.loads(zlib.decompress(base64.b16decode(stdout.strip()[2:-1])))
    left, right, down, running, jumping = args.representation.interpret(cfg)
    fm2 = fm2_smb(left, right, down, running, jumping)
    _, _, framecount = run_movie(fm2, args)
    print(fm2_smb(left, right, down, running, jumping, header=False, maxFrame=framecount))

if __name__ == '__main__':
  args = argparser.parse_args()
  if args.tuning_run:
    if args.database is not None:
      new_bests_movie(args)
    else:
      print("must specify --database")
  else:
    if os.path.isfile('smb.nes'):
      SMBMI.main(args)
    else:
      print("smb.nes not found")

