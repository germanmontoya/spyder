# -*- coding: utf-8 -*-
#
# Copyright © Spyder Project Contributors
# Licensed under the terms of the MIT License
# (see spyder/__init__.py for details)

"""Utilities for searching files."""

import json

def process_ripgrep_json(result):
    cont = 0
    position_string = -1
    flag_first_bracket = 0
    all_string_json_objects = []


    result = result.replace('\n', '\\n' )
    result = result.replace('\t', '\\t' )
    for each_character in result:
        position_string = position_string + 1

        if each_character == '{':
            cont = cont + 1
            if flag_first_bracket == 0:
                pos_start_object = position_string
                flag_first_bracket = 1
        if each_character == '}':
            cont = cont - 1
            if cont==0:
                pos_end_object = position_string
                flag_first_bracket = 0
                one_string_json_object = result[pos_start_object:pos_end_object + 1]
                all_string_json_objects.append(one_string_json_object)

    return all_string_json_objects


def process_ripgrep_output(result):
    all_string_json_objects = process_ripgrep_json(result)
    list_dict = []
    if len(all_string_json_objects) != 0:
        cont_matches = 0
        for each_string_json_object in all_string_json_objects:
            result_dict = json.loads(each_string_json_object)
            if result_dict['type'] == 'match':
                cont_matches = cont_matches + 1
                each_dict={'path':result_dict['data']['path']['text'],
                           'match':result_dict['data']['submatches'][0]['match']['text'],
                           'line':result_dict['data']['lines']['text'],
                           'row':result_dict['data']['line_number'],
                           'column':result_dict['data']['submatches'][0]['end'] + 2}
                list_dict.append(each_dict)
    list_of_matches=list_dict
    return list_of_matches

