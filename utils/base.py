#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: base.py
@time: 2020/3/23 17:09
@desc:
'''

import random

def random_string(length=8):
    seed = "1234567890abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ!@#$%^&*()_+=-"
    sa = []
    for i in range(length):
        sa.append(random.choice(seed))
    salt = ''.join(sa)
    return salt


def reg_trim(data):
    result = []

    for i in data:
        if type(i) is tuple:
            for j in i:
                if j:
                    result.append(j.strip())

            continue

        if i:
            result.append(i.strip())

    return result


def check_target(target_list):
    result = []

    for target in target_list.split(','):
        if target.strip():

            if '\n' in target:
                for t in target.split('\n'):
                    if t.strip():
                        if t.strip().startswith("http://") or t.strip().startswith("https://"):
                            result.append(t.strip())
                        else:
                            result.append('http://' + t.strip())

            else:
                if target.strip().startswith("http://"):
                    result.append(target.strip())
                else:
                    if target.strip():
                        result.append('http://'+target.strip())

    return result
