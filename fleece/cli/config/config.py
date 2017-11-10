#!/usr/bin/env python
import argparse
import base64
import json
import os
import subprocess
import sys

import boto3
import ruamel.yaml as yaml
import six

from fleece.cli.run import run

if six.PY2:
    input = raw_input


class AWSCredentialCache(object):
    def __init__(self, rs_username, rs_api_key, env_config):
        self.rs_username = rs_username
        self.rs_api_key = rs_api_key
        self.environments = run.get_config(env_config)['environments']
        self.rax_token = None
        self.tenant = None
        self.awscreds = {}

    def _get_rax_token(self):
        if self.rax_token is None:
            self.rax_token, self.tenant = run.get_rackspace_token(
                self.rs_username, self.rs_api_key)
        return self.rax_token, self.tenant

    def get_awscreds(self, environment):
        if environment not in self.awscreds:
            account = None
            for env in self.environments:
                if env['name'] == environment:
                    account = env['account']
                    break
            if account is None:
                raise ValueError('Environment "{}" is not known, add it to '
                                 'environments.yml file'.format(environment))
            token, tenant = self._get_rax_token()
            self.awscreds[environment] = run.get_aws_creds(
                account, tenant, token)
        return self.awscreds[environment]


STATE = {
    'awscreds': None,  # cache of aws credentials
    'keys': {}         # kms key for each environment
}


def _encrypt_text(text, environment):
    if environment not in STATE['keys']:
        raise ValueError('No key defined for environment "{}"'.format(
            environment))
    awscreds = STATE['awscreds'].get_awscreds(environment)
    kms = boto3.client('kms', aws_access_key_id=awscreds['accessKeyId'],
                       aws_secret_access_key=awscreds['secretAccessKey'],
                       aws_session_token=awscreds['sessionToken'])
    r = kms.encrypt(KeyId='alias/' + STATE['keys'][environment],
                    Plaintext=text.encode('utf-8'))
    return base64.b64encode(r['CiphertextBlob']).decode('utf-8')


def _decrypt_text(text, environment):
    awscreds = STATE['awscreds'].get_awscreds(environment)
    kms = boto3.client('kms', aws_access_key_id=awscreds['accessKeyId'],
                       aws_secret_access_key=awscreds['secretAccessKey'],
                       aws_session_token=awscreds['sessionToken'])
    r = kms.decrypt(CiphertextBlob=base64.b64decode(text.encode('utf-8')))
    return r['Plaintext'].decode('utf-8')


def _encrypt_item(data, stage, key):
    if (isinstance(data, six.text_type) or isinstance(data, six.binary_type)) \
            and data.startswith(':encrypt:'):
        if not stage:
            sys.stderr.write('Warning: Key "{}" cannot be encrypted because '
                             'it does not belong to a stage\n'.format(key))
        else:
            data = ':decrypt:' + _encrypt_text(data[9:], stage)
    elif isinstance(data, dict):
        per_stage = [k.startswith('-') for k in data]
        if any(per_stage):
            if not all(per_stage):
                raise ValueError('Keys "{}" have a mix of stage and non-stage '
                                 'variables'.format(', '.join(data.keys)))
            key_prefix = key + '.' if key else ''
            for k, v in data.items():
                data[k] = _encrypt_item(v, stage=k[1:], key=key_prefix + k)
        else:
            data = _encrypt_dict(data, stage=stage, key=key)
    elif isinstance(data, list):
        data = _encrypt_list(data, stage=stage, key=key)
    return data


def _encrypt_list(data, stage, key):
    return [_encrypt_item(v, stage=stage, key=key + '[]') for v in data]


def _encrypt_dict(data, stage=None, key=''):
    key_prefix = key + '.' if key else ''
    for k, v in data.items():
        data[k] = _encrypt_item(v, stage=stage, key=key_prefix + k)
    return data


def import_config(args):
    source = sys.stdin.read().strip()
    if source[0] == '{':
        # JSON input
        config = json.loads(source)
    else:
        # YAML input
        config = yaml.round_trip_load(source)

    STATE['keys'] = config['keys']
    config['config'] = _encrypt_dict(config['config'])
    with open(args.config, 'wt') as f:
        if config:
            yaml.round_trip_dump(config, f)


def _decrypt_item(data, stage, key, render):
    if (isinstance(data, six.text_type) or isinstance(data, six.binary_type)) \
            and data.startswith(':decrypt:'):
        data = _decrypt_text(data[9:], stage)
        if not render:
            data = ':encrypt:' + data
    elif isinstance(data, dict):
        per_stage = [k.startswith('-') for k in data]
        if any(per_stage):
            if not all(per_stage):
                raise ValueError('Keys "{}" have a mix of stage and non-stage '
                                 'variables'.format(', '.join(data.keys)))
        if render:
            main_stage, default_stage = (stage + ':').split(':')[:2]
            if per_stage[0]:
                if '-' + main_stage in data:
                    data = _decrypt_item(
                        data.get(stage, data['-' + main_stage]),
                        stage=stage, key=key, render=render)
                elif '-' + default_stage in data:
                    data = _decrypt_item(
                        data.get(stage, data['-' + default_stage]),
                        stage=stage, key=key, render=render)
                else:
                    raise ValueError('Key "{}" has no value for stage '
                                     '"{}"'.format(key, stage))
            else:
                data = _decrypt_dict(data, stage=stage, key=key, render=render)
        else:
            key_prefix = key + '.' if key else ''
            for k, v in data.items():
                data[k] = _decrypt_item(v, stage=k[1:], key=key_prefix + k,
                                        render=render)
            data = _decrypt_dict(data, stage=stage, key=key, render=render)
    elif isinstance(data, list):
        data = _decrypt_list(data, stage=stage, key=key, render=render)
    return data


def _decrypt_list(data, stage, key, render):
    return [_decrypt_item(v, stage=stage, key=key + '[]', render=render)
            for v in data]


def _decrypt_dict(data, stage=None, key='', render=False):
    key_prefix = key + '.' if key else ''
    for k, v in data.items():
        data[k] = _decrypt_item(v, stage=stage, key=key_prefix + k,
                                render=render)
    return data


def export_config(args):
    if os.path.exists(args.config):
        with open(args.config, 'rt') as f:
            config = yaml.round_trip_load(f.read())
        config['config'] = _decrypt_dict(config['config'])
    else:
        config = {'keys': {env['name']: 'enter-key-name-here'
                           for env in STATE['awscreds'].environments},
                  'config': {}}
    if args.json:
        print(json.dumps(config, indent=4))
    elif config:
        yaml.round_trip_dump(config, sys.stdout)


def edit_config(args):
    filename = '.fleece_render_tmp'
    skip_import = False

    if os.path.exists(filename):
        p = input('A previously interrupted edit session was found. Do you '
                  'want to (C)ontinue that session or (A)bort it? ')
        if p.lower() == 'a':
            os.unlink(filename)
        elif p.lower() == 'c':
            skip_import = True

    if not skip_import:
        with open(filename, 'wt') as fd:
            stdout = sys.stdout
            sys.stdout = fd
            export_config(args)
            sys.stdout = stdout

    subprocess.call(args.editor + ' ' + filename, shell=True)

    with open(filename, 'rt') as fd:
        stdin = sys.stdin
        sys.stdin = fd
        import_config(args)
        sys.stdin = stdin

    os.unlink(filename)


def render_config(args):
    with open(args.config, 'rt') as f:
        config = yaml.safe_load(f.read())
    config = _decrypt_item(config, stage=args.stage, key='', render=True)
    if args.json:
        print(json.dumps(config['config'], indent=4))
    elif config:
        yaml.round_trip_dump(config['config'], sys.stdout)


def upload_config(args):
    print('not implemented yet')


def parse_args(args):
    parser = argparse.ArgumentParser(
        prog='fleece config',
        description=('Configuration management')
    )
    parser.add_argument('--config', '-c', default='config.yml',
                        help='Config file (default is config.yml)')
    parser.add_argument('--username', '-u', type=str,
                        default=os.environ.get('RS_USERNAME'),
                        help=('Rackspace username. Can also be set via '
                              'RS_USERNAME environment variable'))
    parser.add_argument('--apikey', '-k', type=str,
                        default=os.environ.get('RS_API_KEY'),
                        help=('Rackspace API key. Can also be set via '
                              'RS_API_KEY environment variable'))
    parser.add_argument('--environments', '-e', type=str,
                        default='./environments.yml',
                        help=('Path to YAML config file with defined accounts '
                              'and stage names. Defaults to '
                              './environments.yml'))
    subparsers = parser.add_subparsers(help='Sub-command help')

    import_parser = subparsers.add_parser(
        'import', help='Import configuration from stdin')
    import_parser.set_defaults(func=import_config)

    export_parser = subparsers.add_parser(
        'export', help='Export configuration to stdout')
    export_parser.add_argument(
        '--json', action='store_true',
        help='Use JSON format (default is YAML)')
    export_parser.set_defaults(func=export_config)

    edit_parser = subparsers.add_parser(
        'edit', help='Edit configuration')
    edit_parser.add_argument(
        '--json', action='store_true',
        help='Use JSON format (default is YAML)')
    edit_parser.add_argument(
        '--editor', '-e', default=os.environ.get('FLEECE_EDITOR', 'vi'),
        help='Text editor (defaults to $FLEECE_EDITOR, or else "vi")')
    edit_parser.set_defaults(func=edit_config)

    render_parser = subparsers.add_parser(
        'render', help='Render configuration for a stage')
    render_parser.add_argument(
        '--json', action='store_true',
        help='Use JSON format (default is YAML)')
    render_parser.add_argument(
        'stage', help='Target stage name')
    render_parser.set_defaults(func=render_config)

    upload_parser = subparsers.add_parser(
        'upload', help='Upload configuration to SSM')
    upload_parser.set_defaults(func=upload_config)
    return parser.parse_args(args)


def main(args):
    parsed_args = parse_args(args)

    STATE['awscreds'] = AWSCredentialCache(rs_username=parsed_args.username,
                                           rs_api_key=parsed_args.apikey,
                                           env_config=parsed_args.environments)
    parsed_args.func(parsed_args)