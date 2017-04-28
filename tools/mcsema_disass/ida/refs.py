# Copyright (c) 2017 Trail of Bits, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from util import *

class Reference(object):
  __slots__ = ('offset', 'addr', 'symbol', 'type')

  INVALID = 0
  IMMEDIATE = 1
  DISPLACEMENT = 2
  MEMORY = 3
  CODE = 4

  TYPE_TO_STR = {
    INVALID: "(null)",
    IMMEDIATE: "imm",
    DISPLACEMENT: "disp",
    MEMORY: "mem",
    CODE: "code",
  }

  def __init__(self, addr, offset):
    self.offset = offset
    self.addr = addr
    self.symbol = ""
    self.type = self.INVALID

  def __str__(self):
    return "({} {} {})".format(
      is_code(self.addr) and "code" or "data",
      self.TYPE_TO_STR[self.type],
      self.symbol or "0x{:x}".format(self.addr))

# Try to recognize an operand as a reference candidate when a target fixup
# is not available.
def _get_ref_candidate(op, all_refs):
  ref = None
  if idc.o_imm == op.type:
    if op.value in all_refs:
      ref = Reference(op.value, op.offb)
      return ref

  elif op.type in (idc.o_displ, idc.o_mem, idc.o_near):
    if op.addr in all_refs:
      ref = Reference(op.addr, op.offb)
      return ref

  return ref

_REFS = {}
_HAS_NO_REFS = set()
_NO_REFS = tuple()

def memop_is_actually_displacement(inst):
  """IDA will unhelpfully decode something like `jmp ds:off_48A5F0[rax*8]`
  and tell us that this is an `o_mem` rather than an `o_displ`. We really want
  to recognize it as an `o_displ` because the memory reference is a displacement
  and not an absolute address."""
  asm = idc.GetDisasm(inst.ea)
  return "[" in asm and ("+" in asm or "*" in asm)

# Get a list of references from an instruction.
def get_instruction_references(arg, binary_is_pie=False):
  inst = arg
  if isinstance(arg, (int, long)):
    inst, _ = decode_instruction(arg)
  
  if not inst or inst.ea in _HAS_NO_REFS:
    return _NO_REFS

  if inst.ea in _REFS:
    return _REFS[inst.ea]

  offset_to_ref = {}
  all_refs = set()
  for ea in xrange(inst.ea, inst.ea + inst.size):
    targ = idc.GetFixupTgtOff(ea)
    if targ != idc.BADADDR and targ != -1:
      all_refs.add(targ)
      ref = Reference(targ, ea - inst.ea)
      offset_to_ref[ref.offset] = ref

  all_refs.update(long(x) for x in idautils.DataRefsFrom(inst.ea))
  all_refs.update(long(x) for x in idautils.CodeRefsFrom(inst.ea, 0))
  all_refs.update(long(x) for x in idautils.CodeRefsFrom(inst.ea, 1))

  refs = []
  for i, op in enumerate(inst.Operands):
    if not op.type:
      continue

    op_ea = inst.ea + op.offb
    if op.offb in offset_to_ref:
      ref = offset_to_ref[op.offb]
    else:
      ref = _get_ref_candidate(op, all_refs)

    if not ref or not idc.GetFlags(ref.addr):
      continue

    # Immediate constant, may be the absolute address of a data reference.
    if idc.o_imm == op.type:
      seg_begin = idaapi.getseg(ref.addr)
      seg_end = idaapi.getseg(ref.addr + idc.ItemSize(ref.addr) - 1)

      # If the immediate constant is not within a segment, or crosses
      # two segments then don't treat it as a reference.
      if not seg_begin or not seg_end or seg_begin.startEA != seg_end.startEA:
        idaapi.del_dref(op_ea, op.value)
        idaapi.del_cref(op_ea, op.value, False)
        continue

      # If this is a PIE-mode, 64-bit binary, then most likely the immediate
      # operand is not a data ref. 
      if seg_begin.use64() and binary_is_pie:
        idaapi.del_dref(op_ea, op.value)
        idaapi.del_cref(op_ea, op.value, False)
        continue

      ref.type = Reference.IMMEDIATE
      ref.symbol = get_symbol_name(op_ea, ref.addr)

    # Displacement within a memory operand, excluding PC-relative
    # displacements when those are memory references.
    elif idc.o_displ == op.type:
      assert ref.addr == op.addr
      ref.type = Reference.DISPLACEMENT
      ref.symbol = get_symbol_name(op_ea, ref.addr)

    # Absolute memory reference, and PC-relative memory reference. These
    # are references that IDA can recognize statically.
    elif idc.o_mem == op.type:
      assert ref.addr == op.addr
      if memop_is_actually_displacement(inst):
        ref.type = Reference.DISPLACEMENT
      else:
        ref.type = Reference.MEMORY
      ref.symbol = get_symbol_name(op_ea, ref.addr)

    # Code reference.
    elif idc.o_near == op.type:
      assert ref.addr == op.addr
      ref.type = Reference.CODE
      ref.symbol = get_symbol_name(op_ea, ref.addr)

    # TODO(pag): Not sure what to do with this yet.
    elif idc.o_far == op.type:
      DEBUG("ERROR inst={:x}\ntarget={:x}\nsym={}".format(
          inst.ea, ref.addr, get_symbol_name(op_ea, ref.addr)))
      assert False

    refs.append(ref)

  for ref in refs:
    assert ref.addr != idc.BADADDR
  
  if len(refs):
    refs = tuple(refs)
    _REFS[inst.ea] = refs
    return refs
  else:
    _HAS_NO_REFS.add(inst.ea)
    return _NO_REFS