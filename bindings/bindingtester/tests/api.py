#
# api.py
#
# This source file is part of the FoundationDB open source project
#
# Copyright 2013-2018 Apple Inc. and the FoundationDB project authors
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
#

import random

import fdb

from bindingtester import FDB_API_VERSION
from bindingtester.tests import Test, Instruction, InstructionSet, ResultSpecification
from bindingtester.tests import test_util

fdb.api_version(FDB_API_VERSION)

class ApiTest(Test):
    def __init__(self, subspace):
        super(ApiTest, self).__init__(subspace)
        self.workspace = self.subspace['workspace'] # The keys and values here must match between subsequent runs of the same test
        self.scratch = self.subspace['scratch'] # The keys and values here can differ between runs
        self.stack_subspace = self.subspace['stack']

        self.versionstamped_values = self.scratch['versionstamped_values']
        self.versionstamped_keys = self.scratch['versionstamped_keys']

    def setup(self, args):
        self.stack_size = 0
        self.string_depth = 0
        self.key_depth = 0
        self.max_keys = 1000

        self.has_version = False
        self.can_set_version = True
        self.is_committed = True
        self.can_use_key_selectors = True

        self.generated_keys = []
        self.outstanding_ops = []
        self.random = test_util.RandomGenerator(args.max_int_bits)

    def add_stack_items(self, num):
        self.stack_size += num
        self.string_depth = 0
        self.key_depth = 0

    def add_strings(self, num):
        self.stack_size += num
        self.string_depth += num
        self.key_depth = 0

    def add_keys(self, num):
        self.stack_size += num
        self.string_depth += num
        self.key_depth += num

    def remove(self, num):
        self.stack_size -= num
        self.string_depth = max(0, self.string_depth - num)
        self.key_depth = max(0, self.key_depth - num)

        self.outstanding_ops = [i for i in self.outstanding_ops if i[0] <= self.stack_size]
        
    def ensure_string(self, instructions, num):
        while self.string_depth < num:
            instructions.push_args(self.random.random_string(random.randint(0, 100)))
            self.add_strings(1)

        self.remove(num)

    def choose_key(self):
        if random.random() < float(len(self.generated_keys)) / self.max_keys:
            tup = random.choice(self.generated_keys)
            if random.random() < 0.3:
                return self.workspace.pack(tup[0:random.randint(0, len(tup))])

            return self.workspace.pack(tup)

        tup = self.random.random_tuple(5)
        self.generated_keys.append(tup)

        return self.workspace.pack(tup) 

    def ensure_key(self, instructions, num):
        while self.key_depth < num:
            instructions.push_args(self.choose_key())
            self.add_keys(1)

        self.remove(num)

    def ensure_key_value(self, instructions):
        if self.string_depth == 0:
            instructions.push_args(self.choose_key(), self.random.random_string(random.randint(0, 100)))

        elif self.string_depth == 1 or self.key_depth == 0:
            self.ensure_key(instructions, 1)
            self.remove(1)

        else:
            self.remove(2)

    def preload_database(self, instructions, num):
        for i in range(num):
            self.ensure_key_value(instructions)
            instructions.append('SET')

            if i % 100 == 99:
                test_util.blocking_commit(instructions)

        test_util.blocking_commit(instructions)
        self.add_stack_items(1)

    def wait_for_reads(self, instructions):
        while len(self.outstanding_ops) > 0 and self.outstanding_ops[-1][0] <= self.stack_size:
            read = self.outstanding_ops.pop()
            #print '%d. waiting for read at instruction %r' % (len(instructions), read)
            test_util.to_front(instructions, self.stack_size - read[0])
            instructions.append('WAIT_FUTURE')

    def generate(self, args, thread_number):
        instructions = InstructionSet()

        op_choices = ['NEW_TRANSACTION', 'COMMIT']

        reads = ['GET', 'GET_KEY', 'GET_RANGE', 'GET_RANGE_STARTS_WITH', 'GET_RANGE_SELECTOR']
        mutations = ['SET', 'CLEAR', 'CLEAR_RANGE', 'CLEAR_RANGE_STARTS_WITH', 'ATOMIC_OP']
        snapshot_reads = [x + '_SNAPSHOT' for x in reads]
        database_reads = [x + '_DATABASE' for x in reads]
        database_mutations = [x + '_DATABASE' for x in mutations]
        mutations += ['VERSIONSTAMP']
        versions = ['GET_READ_VERSION', 'SET_READ_VERSION', 'GET_COMMITTED_VERSION']
        snapshot_versions = ['GET_READ_VERSION_SNAPSHOT']
        tuples = ['TUPLE_PACK', 'TUPLE_UNPACK', 'TUPLE_RANGE', 'SUB']
        resets = ['ON_ERROR', 'RESET', 'CANCEL']
        read_conflicts = ['READ_CONFLICT_RANGE', 'READ_CONFLICT_KEY']
        write_conflicts = ['WRITE_CONFLICT_RANGE', 'WRITE_CONFLICT_KEY', 'DISABLE_WRITE_CONFLICT']

        op_choices += reads
        op_choices += mutations
        op_choices += snapshot_reads
        op_choices += database_reads
        op_choices += database_mutations
        op_choices += versions
        op_choices += snapshot_versions
        op_choices += tuples
        op_choices += read_conflicts
        op_choices += write_conflicts
        op_choices += resets

        idempotent_atomic_ops = [u'BIT_AND', u'BIT_OR', u'MAX', u'MIN']
        atomic_ops = idempotent_atomic_ops + [u'ADD', u'BIT_XOR']

        if args.concurrency > 1:
            self.max_keys = random.randint(100, 1000)
        else:
            self.max_keys = random.randint(100, 10000)

        instructions.append('NEW_TRANSACTION')
        instructions.append('GET_READ_VERSION')

        self.preload_database(instructions, self.max_keys)

        instructions.setup_complete()

        for i in range(args.num_ops):
            op = random.choice(op_choices)
            index = len(instructions)

            #print 'Adding instruction %s at %d' % (op, index)

            if args.concurrency == 1 and (op in database_mutations):
                self.wait_for_reads(instructions)
                test_util.blocking_commit(instructions)
                self.add_stack_items(1)

            if op in resets or op == 'NEW_TRANSACTION':
                if args.concurrency == 1:
                    self.wait_for_reads(instructions)

                self.outstanding_ops = []

            if op == 'NEW_TRANSACTION':
                instructions.append(op)
                self.is_committed = False
                self.can_set_version = True
                self.can_use_key_selectors = True

            elif op == 'ON_ERROR':
                instructions.push_args(random.randint(0, 5000))
                instructions.append(op)

                self.outstanding_ops.append((self.stack_size, len(instructions)-1))
                if args.concurrency == 1:
                    self.wait_for_reads(instructions)

                instructions.append('NEW_TRANSACTION')
                self.is_committed = False
                self.can_set_version = True
                self.can_use_key_selectors = True
                self.add_strings(1)

            elif op == 'GET' or op == 'GET_SNAPSHOT' or op == 'GET_DATABASE':
                self.ensure_key(instructions, 1)
                instructions.append(op)
                self.add_strings(1)
                self.can_set_version = False

            elif op == 'GET_KEY' or op == 'GET_KEY_SNAPSHOT' or op == 'GET_KEY_DATABASE':
                if op.endswith('_DATABASE') or self.can_use_key_selectors:
                    self.ensure_key(instructions, 1)
                    instructions.push_args(self.workspace.key())
                    instructions.push_args(*self.random.random_selector_params())
                    test_util.to_front(instructions, 3)
                    instructions.append(op)

                    #Don't add key here because we may be outside of our prefix
                    self.add_strings(1)
                    self.can_set_version = False

            elif op == 'GET_RANGE' or op == 'GET_RANGE_SNAPSHOT' or op == 'GET_RANGE_DATABASE':
                self.ensure_key(instructions, 2)
                range_params = self.random.random_range_params()
                instructions.push_args(*range_params)
                test_util.to_front(instructions, 4)
                test_util.to_front(instructions, 4)
                instructions.append(op)

                if range_params[0] >= 1 and range_params[0] <= 1000: # avoid adding a string if the limit is large
                    self.add_strings(1)
                else:
                    self.add_stack_items(1)

                self.can_set_version = False

            elif op == 'GET_RANGE_STARTS_WITH' or op == 'GET_RANGE_STARTS_WITH_SNAPSHOT' or op == 'GET_RANGE_STARTS_WITH_DATABASE':
                #TODO: not tested well
                self.ensure_key(instructions, 1)
                range_params = self.random.random_range_params()
                instructions.push_args(*range_params)
                test_util.to_front(instructions, 3)
                instructions.append(op)

                if range_params[0] >= 1 and range_params[0] <= 1000: # avoid adding a string if the limit is large
                    self.add_strings(1)
                else:
                    self.add_stack_items(1)

                self.can_set_version = False

            elif op == 'GET_RANGE_SELECTOR' or op == 'GET_RANGE_SELECTOR_SNAPSHOT' or op == 'GET_RANGE_SELECTOR_DATABASE':
                if op.endswith('_DATABASE') or self.can_use_key_selectors:
                    self.ensure_key(instructions, 2)
                    instructions.push_args(self.workspace.key())
                    range_params = self.random.random_range_params()
                    instructions.push_args(*range_params)
                    instructions.push_args(*self.random.random_selector_params())
                    test_util.to_front(instructions, 6)
                    instructions.push_args(*self.random.random_selector_params())
                    test_util.to_front(instructions, 9)
                    instructions.append(op)

                    if range_params[0] >= 1 and range_params[0] <= 1000: # avoid adding a string if the limit is large
                        self.add_strings(1)
                    else:
                        self.add_stack_items(1)

                    self.can_set_version = False

            elif op == 'GET_READ_VERSION' or op == 'GET_READ_VERSION_SNAPSHOT':
                instructions.append(op)
                self.has_version = self.can_set_version
                self.add_strings(1)

            elif op == 'SET' or op == 'SET_DATABASE':
                self.ensure_key_value(instructions)
                instructions.append(op)
                if op == 'SET_DATABASE':
                    self.add_stack_items(1)   
                
            elif op == 'SET_READ_VERSION':
                if self.has_version and self.can_set_version:
                    instructions.append(op)
                    self.can_set_version = False

            elif op == 'CLEAR' or op == 'CLEAR_DATABASE':
                self.ensure_key(instructions, 1)
                instructions.append(op)
                if op == 'CLEAR_DATABASE':
                    self.add_stack_items(1)

            elif op == 'CLEAR_RANGE' or op == 'CLEAR_RANGE_DATABASE':
                #Protect against inverted range
                key1 = self.workspace.pack(self.random.random_tuple(5))
                key2 = self.workspace.pack(self.random.random_tuple(5))

                if key1 > key2:
                    key1, key2 = key2, key1

                instructions.push_args(key1, key2)

                instructions.append(op)
                if op == 'CLEAR_RANGE_DATABASE':
                    self.add_stack_items(1)

            elif op == 'CLEAR_RANGE_STARTS_WITH' or op == 'CLEAR_RANGE_STARTS_WITH_DATABASE':
                self.ensure_key(instructions, 1)
                instructions.append(op)
                if op == 'CLEAR_RANGE_STARTS_WITH_DATABASE':
                    self.add_stack_items(1)
                    
            elif op == 'ATOMIC_OP' or op == 'ATOMIC_OP_DATABASE':
                self.ensure_key_value(instructions)
                if op == 'ATOMIC_OP' or args.concurrency > 1:
                    instructions.push_args(random.choice(atomic_ops))
                else:
                    instructions.push_args(random.choice(idempotent_atomic_ops))

                instructions.append(op)
                if op == 'ATOMIC_OP_DATABASE':
                    self.add_stack_items(1)

            elif op == 'VERSIONSTAMP':
                rand_str1 = self.random.random_string(100)
                key1 = self.versionstamped_values.pack((rand_str1,))

                split = random.randint(0, 70)
                rand_str2 = self.random.random_string(20+split) + 'XXXXXXXXXX' + self.random.random_string(70-split)
                key2 = self.versionstamped_keys.pack() + rand_str2
                index = key2.find('XXXXXXXXXX')
                key2 += chr(index%256)+chr(index/256)

                instructions.push_args(u'SET_VERSIONSTAMPED_VALUE', key1, 'XXXXXXXXXX' + rand_str2)
                instructions.append('ATOMIC_OP')

                instructions.push_args(u'SET_VERSIONSTAMPED_KEY', key2, rand_str1)
                instructions.append('ATOMIC_OP')
                self.can_use_key_selectors = False

            elif op == 'READ_CONFLICT_RANGE' or op == 'WRITE_CONFLICT_RANGE':
                self.ensure_key(instructions, 2)
                instructions.append(op)
                self.add_strings(1)

            elif op == 'READ_CONFLICT_KEY' or op == 'WRITE_CONFLICT_KEY':
                self.ensure_key(instructions, 1)
                instructions.append(op)
                self.add_strings(1)

            elif op == 'DISABLE_WRITE_CONFLICT':
                instructions.append(op)

            elif op == 'COMMIT':
                if args.concurrency == 1 or i < self.max_keys or random.random() < 0.9:
                    if args.concurrency == 1:
                        self.wait_for_reads(instructions)
                    test_util.blocking_commit(instructions)
                    self.add_stack_items(1)
                    self.is_committed = True
                    self.can_set_version = True
                    self.can_use_key_selectors = True
                else:
                    instructions.append(op)
                    self.add_strings(1)

            elif op == 'RESET':
                instructions.append(op)
                self.is_committed = False
                self.can_set_version = True
                self.can_use_key_selectors = True

            elif op == 'CANCEL':
                instructions.append(op)
                self.is_committed = False
                self.can_set_version = False

            elif op == 'GET_COMMITTED_VERSION':
                if self.is_committed:
                    instructions.append(op)
                    self.has_version = True
                    self.add_strings(1)

            elif op == 'TUPLE_PACK' or op == 'TUPLE_RANGE':
                tup = self.random.random_tuple(10)
                instructions.push_args(len(tup), *tup)
                instructions.append(op)
                if op == 'TUPLE_PACK':
                    self.add_strings(1)
                else:
                    self.add_strings(2)

            elif op == 'TUPLE_UNPACK':
                tup = self.random.random_tuple(10)
                instructions.push_args(len(tup), *tup)
                instructions.append('TUPLE_PACK')
                instructions.append(op)
                self.add_strings(len(tup))

            #Use SUB to test if integers are correctly unpacked
            elif op == 'SUB':
                a = self.random.random_int() / 2
                b = self.random.random_int() / 2
                instructions.push_args(0, a, b)
                instructions.append(op)
                instructions.push_args(1)
                instructions.append('SWAP')
                instructions.append(op)
                instructions.push_args(1)
                instructions.append('TUPLE_PACK')
                self.add_stack_items(1)

            else:
                assert False

            if op in reads or op in snapshot_reads:
                self.outstanding_ops.append((self.stack_size, len(instructions)-1))

            if args.concurrency == 1 and (op in database_reads or op in database_mutations):
                instructions.append('WAIT_FUTURE')

        instructions.begin_finalization()

        if args.concurrency == 1:
            self.wait_for_reads(instructions)
            test_util.blocking_commit(instructions)
            self.add_stack_items(1)

        instructions.append('NEW_TRANSACTION')
        instructions.push_args(self.stack_subspace.key())
        instructions.append('LOG_STACK')

        test_util.blocking_commit(instructions)

        return instructions

    @fdb.transactional
    def check_versionstamps(self, tr, begin_key, limit):
        next_begin = None
        incorrect_versionstamps = 0
        for k,v in tr.get_range(begin_key, self.versionstamped_values.range().stop, limit=limit):
            next_begin = k + '\x00'
            tup = fdb.tuple.unpack(k)
            key = self.versionstamped_keys.pack() + v[10:].replace('XXXXXXXXXX', v[:10], 1)
            if tr[key] != tup[-1]:
                incorrect_versionstamps += 1

        return (next_begin, incorrect_versionstamps)

    def validate(self, db, args):
        errors = []

        begin = self.versionstamped_values.range().start
        incorrect_versionstamps = 0

        while begin is not None:
            (begin, current_incorrect_versionstamps) = self.check_versionstamps(db, begin, 100)
            incorrect_versionstamps += current_incorrect_versionstamps

        if incorrect_versionstamps > 0:
            errors.append('There were %d failed version stamp operations' % incorrect_versionstamps)

        return errors

    def get_result_specifications(self):
        return [ 
            ResultSpecification(self.workspace, global_error_filter=[1007, 1021]), 
            ResultSpecification(self.stack_subspace, key_start_index=1, ordering_index=1, global_error_filter=[1007, 1021]) 
        ]
