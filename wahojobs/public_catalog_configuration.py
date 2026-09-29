"""Explicit, protected activation; absent by default, no environment fallbacks."""
import json
import re

SOURCE_CONFIGURATION_PATH = '/etc/wahojobs-beta/public-catalog-v1.json'
OPERATING_CONFIGURATION_PATH = '/run/wahojobs-beta/public-catalog-v1.json'


def load_configuration(path):
    from wahojobs.workos_authkit_staging import (
        _validated_external_file, _read_bounded_file, _unique_object, _reject_json_constant)
    path = _validated_external_file(path, configuration=True)
    document = json.loads(_read_bounded_file(path, maximum=4096).decode('utf-8'),
        object_pairs_hook=_unique_object, parse_constant=_reject_json_constant)
    if (type(document) is not dict or set(document) != {'version', 'public_origin', 'gateway_key', 'indexable'}
            or type(document['version']) is not int or document['version'] != 1
            or document['public_origin'] != 'https://www.wahojobs.com'
            or type(document['gateway_key']) is not str
            or not re.fullmatch('[0-9a-f]{64}', document['gateway_key'])
            or type(document['indexable']) is not bool):
        raise ValueError('invalid_public_catalog_configuration')
    return document
