#!/usr/bin/python


"""Manage accounts in an AWS Organization.

Usage:
  awsaccounts report [-d] [--boto-log]
  awsaccounts create (--spec-file FILE) [--exec] [-vd] [--boto-log]
  awsaccounts (-h | --help)
  awsaccounts --version

Modes of operation:
  report         Display organization status report only.
  create         Create new accounts in AWS Org per specifation.

Options:
  -h, --help                 Show this help message and exit.
  --version                  Display version info and exit.
  -s FILE, --spec-file FILE  AWS account specification file in yaml format.
  --exec                     Execute proposed changes to AWS accounts.
  -v, --verbose              Log to activity to STDOUT at log level INFO.
  -d, --debug                Increase log level to 'DEBUG'. Implies '--verbose'.
  --boto-log                 Include botocore and boto3 logs in log stream.

"""


import yaml
import time
from collections import namedtuple

import boto3
import botocore.exceptions
from botocore.exceptions import ClientError
import docopt
from docopt import docopt

import awsorgs.utils
from awsorgs.utils import *
import awsorgs.orgs
from awsorgs.orgs import scan_deployed_accounts


def scan_created_accounts(org):
    """
    Query AWS Organization for accounts with creation status of 'SUCCEEDED'.
    Returns a list of dictionary.
    """
    org.log.debug('running')
    status = org.client.list_create_account_status(States=['SUCCEEDED'])
    created_accounts = status['CreateAccountStatuses']
    while 'NextToken' in status and status['NextToken']:
        org.log.debug("NextToken: %s" % status['NextToken'])
        status = org.client.list_create_account_status(
                States=['SUCCEEDED'],
                NextToken=status['NextToken'])
        created_accounts += status['CreateAccountStatuses']
    return created_accounts


def create_accounts(org):
    """
    Compare deployed_accounts to list of accounts in the accounts spec.
    Create accounts not found in deployed['accounts'].
    """
    for a_spec in org.account_spec['accounts']:
        if not lookup(org.deployed['accounts'], 'Name', a_spec['Name'],):
            # check if it is still being provisioned
            created_accounts = scan_created_accounts(org)
            if lookup(created_accounts, 'AccountName', a_spec['Name']):
                org.log.warn("New account '%s' is not yet available" %
                        a_spec['Name'])
                break
            # create a new account
            if 'Email' in a_spec and a_spec['Email']:
                email_addr = a_spec['Email']
            else:
                email_addr = '%s@%s' % (a_spec['Name'],
                        org.account_spec['default_domain'])
            org.log.info("Creating account '%s'" % (a_spec['Name']))
            org.log.debug('account email: %s' % email_addr)
            if org.args['--exec']:
                new_account = org.client.create_account(
                        AccountName=a_spec['Name'], Email=email_addr)
                create_id = new_account['CreateAccountStatus']['Id']
                org.log.info("CreateAccountStatus Id: %s" % (create_id))
                # validate creation status
                counter = 0
                maxtries = 5
                while counter < maxtries:
                    creation = org.client.describe_create_account_status(
                            CreateAccountRequestId=create_id
                            )['CreateAccountStatus']
                    if creation['State'] == 'IN_PROGRESS':
                        time.sleep(5)
                        org.log.info("Account creation in progress for '%s'" %
                                a_spec['Name'])
                    elif creation['State'] == 'SUCCEEDED':
                        org.log.info("Account creation succeeded")
                        break
                    elif creation['State'] == 'FAILED':
                        org.log.error("Account creation failed: %s" %
                                creation['FailureReason'])
                        break
                    counter += 1
                if counter == maxtries and creation['State'] == 'IN_PROGRESS':
                     org.log.warn("Account creation still pending. Moving on!")


def display_provisioned_accounts(org):
    """
    Print report of currently deployed accounts in AWS Organization.
    """
    header = "Provisioned Accounts in Org:"
    overbar = '_' * len(header)
    org.log.info("\n%s\n%s" % (overbar, header))
    for a_name in sorted(map(lambda a: a['Name'], org.deployed['accounts'])):
        a_id = lookup(org.deployed['accounts'], 'Name', a_name, 'Id')
        a_email = lookup(org.deployed['accounts'], 'Name', a_name, 'Email')
        spacer = ' ' * (24 - len(a_name))
        org.log.info("%s%s%s\t\t%s" % (a_name, spacer, a_id, a_email))


def main():
    # create 'org' object as named tuple.  # initailize all fields to None.
    Org = namedtuple('Org',['args', 'log', 'client', 'deployed', 'account_spec'])
    org = Org(**{f:None for f in Org._fields})

    # populate org attribute values
    org = org._replace(args = docopt(__doc__))
    org = org._replace(client = boto3.client('organizations'))
    org = org._replace(log = get_logger(org.args))
    org = org._replace(deployed = dict())
    org.deployed['accounts'] = scan_deployed_accounts(org.log, org.client)

    if org.args['--spec-file']:
        org = org._replace(account_spec = validate_spec_file(
                org.log, org.args['--spec-file'], 'account_spec'))
        validate_master_id(org.client, org.account_spec)

    if org.args['report']:
        display_provisioned_accounts(org)

    if org.args['create']:
        create_accounts(org)
        unmanaged= [a for a in map(lambda a: a['Name'], org.deployed['accounts'])
                if a not in map(lambda a: a['Name'], org.account_spec['accounts'])]
        if unmanaged:
            log.warn("Unmanaged accounts in Org: %s" % (', '.join(unmanaged)))


if __name__ == "__main__":
    main()
