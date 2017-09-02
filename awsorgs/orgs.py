#!/usr/bin/python


"""Manage recources in an AWS Organization.

Usage:
  awsorgs report [-d] [--boto-log]
  awsorgs organization (--spec-file FILE) [--exec] [-vd] [--boto-log]
  awsorgs --version
  awsorgs --help

Modes of operation:
  report         Display organization status report only.
  orgnanizaion   Run AWS Org management tasks per specification.

Options:
  -h, --help                 Show this help message and exit.
  --version                  Display version info and exit.
  -s FILE, --spec-file FILE  AWS Org specification file in yaml format.
  --exec                     Execute proposed changes to AWS Org.
  -v, --verbose              Log to activity to STDOUT at log level INFO.
  -d, --debug                Increase log level to 'DEBUG'. Implies '--verbose'.
  --boto-log                 Include botocore and boto3 logs in log stream.

"""


import yaml
import json
import time
from collections import namedtuple

import boto3
from docopt import docopt

import awsorgs.utils
from awsorgs.utils import *


def validate_accounts_unique_in_org(org, root_spec):
    """
    Make sure accounts are unique across org
    """
    # recursively build mapping of accounts to ou_names
    def map_accounts(spec, account_map={}):
        if 'Accounts' in spec and spec['Accounts']:
            for account in spec['Accounts']:
                if account in account_map:
                    account_map[account].append(spec['Name'])
                else:
                    account_map[account] = [(spec['Name'])]
        if 'Child_OU' in spec and spec['Child_OU']:
            for child_spec in spec['Child_OU']:
                map_accounts(child_spec, account_map)
        return account_map
    # find accounts set to more than one OU
    unique = True
    for account, ou in map_accounts(root_spec).items():
        if len(ou) > 1:
            org.log.error("Account '%s' set in multiple OU: %s" % (account, ou))
            unique = False
    if not unique:
        org.log.critical("Invalid org_spec: Do not assign accounts to multiple "
                "Organizatinal Units")
        sys.exit(1)


def enable_policy_type_in_root(org):
    """
    Ensure policy type 'SERVICE_CONTROL_POLICY' is enabled in the
    organization root.
    """
    p_type = org.client.describe_organization()['Organization']['AvailablePolicyTypes'][0]
    if (p_type['Type'] == 'SERVICE_CONTROL_POLICY' and p_type['Status'] != 'ENABLED'):
        org.client.enable_policy_type(RootId=org.root_id,
                PolicyType='SERVICE_CONTROL_POLICY')


def get_parent_id(org_client, account_id):
    """
    Query deployed AWS organanization for 'account_id. Return the 'Id' of
    the parent OrganizationalUnit or 'None'.
    """
    parents = org_client.list_parents(ChildId=account_id)['Parents']
    try:
        len(parents) == 1
        return parents[0]['Id']
    except:
        raise RuntimeError("API Error: account '%s' has more than one parent: "
                % (account_id, parents))


def list_policies_in_ou (org_client, ou_id):
    """
    Query deployed AWS organanization.  Return a list (of type dict)
    of policies attached to OrganizationalUnit referenced by 'ou_id'.
    """
    policies_in_ou = org_client.list_policies_for_target(
            TargetId=ou_id, Filter='SERVICE_CONTROL_POLICY',)['Policies']
    return sorted(map(lambda ou: ou['Name'], policies_in_ou))


def scan_deployed_accounts(org):
    """
    Query AWS Organization for deployed accounts.
    Returns a list of dictionary.
    """
    org.log.debug('running')
    accounts = org.client.list_accounts()
    deployed_accounts = accounts['Accounts']
    while 'NextToken' in accounts and accounts['NextToken']:
        org.log.debug("NextToken: %s" % accounts['NextToken'])
        accounts = org.client.list_accounts(NextToken=accounts['NextToken'])
        deployed_accounts += accounts['Accounts']
    # only return accounts that have an 'Name' key
    return [d for d in deployed_accounts if 'Name' in d ]


def scan_created_accounts(org_client):
    """
    Query AWS Organization for accounts with creation status of 'SUCCEEDED'.
    Returns a list of dictionary.
    """
    status = org_client.list_create_account_status(
            States=['SUCCEEDED'])
    created_accounts = status['CreateAccountStatuses']
    while 'NextToken' in status and status['NextToken']:
        status = org_client.list_create_account_status(
                States=['SUCCEEDED'],
                NextToken=status['NextToken'])
        created_accounts += status['CreateAccountStatuses']
    return created_accounts


def scan_deployed_policies(org):
    """
    Return list of Service Control Policies deployed in Organization
    """
    return org.client.list_policies(Filter='SERVICE_CONTROL_POLICY')['Policies']


def scan_deployed_ou(org):
    """
    Recursively traverse deployed AWS Organization.  Return list of
    organizational unit dictionaries.  
    """
    def build_deployed_ou_table(org, parent_name, parent_id, deployed_ou):
        # recusive sub function to build the 'deployed_ou' table
        child_ou = org.client.list_organizational_units_for_parent(
                ParentId=parent_id)['OrganizationalUnits']
        accounts = org.client.list_accounts_for_parent(
                ParentId=parent_id)['Accounts']
        if not deployed_ou:
            deployed_ou.append(dict(
                    Name = parent_name,
                    Id = parent_id,
                    Child_OU = [ou['Name'] for ou in child_ou if 'Name' in ou],
                    Accounts = [acc['Name'] for acc in accounts if 'Name' in acc]))
        else:
            for ou in deployed_ou:
                if ou['Name'] == parent_name:
                    ou['Child_OU'] = map(lambda d: d['Name'], child_ou)
                    ou['Accounts'] = map(lambda d: d['Name'], accounts)
        for ou in child_ou:
            ou['ParentId'] = parent_id
            deployed_ou.append(ou)
            build_deployed_ou_table(org, ou['Name'], ou['Id'], deployed_ou)
    # build the table 
    deployed_ou = []
    build_deployed_ou_table(org, 'root', org.root_id, deployed_ou)
    return deployed_ou


def display_provisioned_policies(org):
    """
    Print report of currently deployed Service Control Policies in
    AWS Organization.
    """
    header = "Provisioned Service Control Policies:"
    overbar = '_' * len(header)
    org.log.info("\n\n%s\n%s" % (overbar, header))
    for policy in org.deployed['policies']:
        org.log.info("\nName:\t\t%s" % policy['Name'])
        org.log.info("Description:\t%s" % policy['Description'])
        org.log.info("Id:\t%s" % policy['Id'])
        org.log.info("Content:")
        org.log.info(json.dumps(json.loads(org.client.describe_policy(
                PolicyId=policy['Id'])['Policy']['Content']),
                indent=2,
                separators=(',', ': ')))


def display_provisioned_ou(org, deployed_ou, parent_name, indent=0):
    """
    Recursive function to display the deployed AWS Organization structure.
    """
    # query aws for child orgs
    parent_id = lookup(deployed_ou, 'Name', parent_name, 'Id')
    child_ou_list = lookup(deployed_ou, 'Name', parent_name, 'Child_OU')
    child_accounts = lookup(deployed_ou, 'Name', parent_name, 'Accounts')
    # display parent ou name
    tab = '  '
    org.log.info(tab*indent + parent_name + ':')
    # look for policies
    policy_names = list_policies_in_ou(org.client, parent_id)
    if len(policy_names) > 0:
        org.log.info(tab*indent + tab + 'Policies: ' + ', '.join(policy_names))
    # look for accounts
    account_list = sorted(child_accounts)
    if len(account_list) > 0:
        org.log.info(tab*indent + tab + 'Accounts: ' + ', '.join(account_list))
    # look for child OUs
    if child_ou_list:
        org.log.info(tab*indent + tab + 'Child_OU:')
        indent+=2
        for ou_name in child_ou_list:
            # recurse
            display_provisioned_ou(org, deployed_ou, ou_name, indent)


def manage_account_moves(org, ou_spec, dest_parent_id):
    """
    Alter deployed AWS Organization.  Ensure accounts are contained
    by designated OrganizationalUnits based on OU specification.
    """
    if 'Accounts' in ou_spec and ou_spec['Accounts']:
        for account in ou_spec['Accounts']:
            account_id = lookup(org.deployed['accounts'], 'Name', account, 'Id')
            if not account_id:
                org.log.warn("Account '%s' not yet in Organization" % account)
            else:
                source_parent_id = get_parent_id(org.client, account_id)
                if dest_parent_id != source_parent_id:
                    org.log.info("Moving account '%s' to OU '%s'" %
                            (account, ou_spec['Name']))
                    if org.args['--exec']:
                        org.client.move_account(
                                AccountId=account_id,
                                SourceParentId=source_parent_id,
                                DestinationParentId=dest_parent_id)


def place_unmanged_accounts(org, account_list):
    """
    Move any unmanaged accounts into the default OU.
    """
    default_ou = org.org_spec['default_ou']
    for account in account_list:
        account_id = lookup(org.deployed['accounts'], 'Name', account, 'Id')
        default_ou_id = lookup(org.deployed['ou'], 'Name',
                org.org_spec['default_ou'], 'Id')
        source_parent_id = get_parent_id(org.client, account_id)
        if default_ou_id and default_ou_id != source_parent_id:
            org.log.info("Moving unmanged account '%s' to default OU '%s'" %
                    (account, default_ou))
            if org.args['--exec']:
                org.client.move_account(
                        AccountId=account_id,
                        SourceParentId=source_parent_id,
                        DestinationParentId=default_ou_id)


def manage_policies(org):
    """
    Manage Service Control Policies in the AWS Organization.  Make updates
    according to the sc_policies specification.  Do not touch
    the default policy.  Do not delete an attached policy.
    """
    for p_spec in org.org_spec['sc_policies']:
        policy_name = p_spec['Name']
        org.log.debug("considering sc_policy: %s" % policy_name)
        # dont touch default policy
        if policy_name == org.org_spec['default_policy']:
            continue
        policy = lookup(org.deployed['policies'], 'Name', policy_name)
        # delete existing sc_policy
        if ensure_absent(p_spec):
            if policy:
                org.log.info("Deleting policy '%s'" % (policy_name))
                # dont delete attached policy
                if org.client.list_targets_for_policy( PolicyId=policy_id)['Targets']:
                    org.log.error("Cannot delete policy '%s'. Still attached to OU" %
                            policy_name)
                elif org.args['--exec']:
                    org.client.delete_policy(PolicyId=policy['Id'])
            continue
        # create or update sc_policy
        statement = dict(Effect=p_spec['Effect'], Action=p_spec['Actions'], Resource='*')
        policy_doc = json.dumps(dict(Version='2012-10-17', Statement=[statement]))
        org.log.debug("spec sc_policy_doc: %s" % policy_doc)
        # create new policy
        if not policy:
            org.log.info("Creating policy '%s'" % policy_name)
            if org.args['--exec']:
                org.client.create_policy(
                        Content=policy_doc,
                        Description=p_spec['Description'],
                        Name=p_spec['Name'],
                        Type='SERVICE_CONTROL_POLICY')
        # check for policy updates
        else:
            deployed_policy_doc = json.dumps(json.loads(org.client.describe_policy(
                    PolicyId=policy['Id'])['Policy']['Content']))
            org.log.debug("real sc_policy_doc: %s" % deployed_policy_doc)
            if (p_spec['Description'] != policy['Description']
                or policy_doc != deployed_policy_doc):
                org.log.info("Updating policy '%s'" % policy_name)
                if org.args['--exec']:
                    org.client.update_policy(
                            PolicyId=policy['Id'],
                            Content=policy_doc,
                            Description=p_spec['Description'],)


def manage_policy_attachments(org, ou_spec, ou_id):
    """
    Attach or detach specified Service Control Policy to a org.deployed 
    OrganizatinalUnit.  Do not detach the default policy ever.
    """
    # create lists policies_to_attach and policies_to_detach
    attached_policy_list = list_policies_in_ou(org.client, ou_id)
    if 'SC_Policies' in ou_spec and isinstance(ou_spec['SC_Policies'], list):
        spec_policy_list = ou_spec['SC_Policies']
    else:
        spec_policy_list = []
    policies_to_attach = [p for p in spec_policy_list
            if p not in attached_policy_list]
    policies_to_detach = [p for p in attached_policy_list
            if p not in spec_policy_list
            and p != org.org_spec['default_policy']]
    # attach policies
    for policy_name in policies_to_attach:
        if not lookup(org.deployed['policies'],'Name',policy_name):
            raise RuntimeError("spec-file: ou_spec: policy '%s' not defined" %
                    policy_name)
        if not ensure_absent(ou_spec):
            org.log.info("Attaching policy '%s' to OU '%s'" %
                    (policy_name, ou_spec['Name']))
            if org.args['--exec']:
                org.client.attach_policy(
                        PolicyId=lookup(
                                org.deployed['policies'], 'Name', policy_name, 'Id'),
                        TargetId=ou_id)
    # detach policies
    for policy_name in policies_to_detach:
        org.log.info("Detaching policy '%s' from OU '%s'" %
                (policy_name, ou_spec['Name']))
        if org.args['--exec']:
            org.client.detach_policy(
                    PolicyId=lookup(org.deployed['policies'], 'Name', policy_name, 'Id'),
                    TargetId=ou_id)


def manage_ou(org, ou_spec_list, parent_name):
    """
    Recursive function to manage OrganizationalUnits in the AWS
    Organization.
    """
    for ou_spec in ou_spec_list:
        # ou exists
        ou = lookup(org.deployed['ou'], 'Name', ou_spec['Name'])
        if ou:
            # check for child_ou. recurse before other tasks.
            if 'Child_OU' in ou_spec:
                manage_ou(org, ou_spec['Child_OU'], ou_spec['Name'])
            # check if ou 'absent'
            if ensure_absent(ou_spec):
                org.log.info("Deleting OU %s" % ou_spec['Name'])
                # error if ou contains anything
                error_flag = False
                for key in ['Accounts', 'SC_Policies', 'Child_OU']:
                    if key in ou and ou[key]:
                        org.log.error("Can not delete OU '%s'. deployed '%s' exists." %
                                (ou_spec['Name'], key))
                        error_flag = True
                if error_flag:
                    continue
                elif org.args['--exec']:
                    org.client.delete_organizational_unit(OrganizationalUnitId=ou['Id'])
            # manage account and sc_policy placement in OU
            else:
                manage_policy_attachments(org, ou_spec, ou['Id'])
                manage_account_moves(org, ou_spec, ou['Id'])
        # create new OU
        elif not ensure_absent(ou_spec):
            org.log.info("Creating new OU '%s' under parent '%s'" %
                    (ou_spec['Name'], parent_name))
            if org.args['--exec']:
                new_ou = org.client.create_organizational_unit(
                        ParentId=lookup(org.deployed['ou'],'Name',parent_name,'Id'),
                        Name=ou_spec['Name'])['OrganizationalUnit']
                # account and sc_policy placement
                manage_policy_attachments(org, ou_spec, new_ou['Id'])
                manage_account_moves(org, ou_spec, new_ou['Id'])
                # recurse if child OU
                if ('Child_OU' in ou_spec and isinstance(new_ou, dict)
                        and 'Id' in new_ou):
                    manage_ou(org, ou_spec['Child_OU'], new_ou['Name'])


def main():
    # create 'org' object as named tuple.  # initailize all fields to None.
    Org = namedtuple('Org',['args', 'log', 'client', 'deployed', 'org_spec', 'root_id'])
    org = Org(**{f:None for f in Org._fields})

    # populate org attribute values
    org = org._replace(args = docopt(__doc__))
    org = org._replace(client = boto3.client('organizations'))
    org = org._replace(log = get_logger(org.args))
    org = org._replace(root_id = get_root_id(org.client))
    org = org._replace(deployed = dict(
            policies = scan_deployed_policies(org),
            accounts = scan_deployed_accounts(org),
            ou = scan_deployed_ou(org)))

    if org.args['--spec-file']:
        org = org._replace(org_spec = validate_spec_file(org.log,
                org.args['--spec-file'], 'org_spec'))
        root_spec = lookup(org.org_spec['organizational_units'], 'Name', 'root')
        validate_master_id(org.client, org.org_spec)
        validate_accounts_unique_in_org(org, root_spec)
        managed = dict(
                accounts = search_spec(root_spec, 'Accounts', 'Child_OU'),
                ou = search_spec(root_spec, 'Name', 'Child_OU'),
                policies = [p['Name'] for p in org.org_spec['sc_policies']])
        # ensure default_policy is considered 'managed'
        if org.org_spec['default_policy'] not in managed['policies']:
            managed['policies'].append(org.org_spec['default_policy'])

    if org.args['report']:
        header = 'Provisioned Organizational Units in Org:'
        overbar = '_' * len(header)
        org.log.info("\n%s\n%s" % (overbar, header))
        display_provisioned_ou(org, org.deployed['ou'], 'root')
        display_provisioned_policies(org)

    if org.args['organization']:
        enable_policy_type_in_root(org)
        manage_policies(org)
        # rescan deployed policies
        org.deployed['policies'] = scan_deployed_policies(org)
        manage_ou(org, org.org_spec['organizational_units'], 'root')
        # check for unmanaged resources
        for key in managed.keys():
            unmanaged= [a['Name'] for a in org.deployed[key]
                    if a['Name'] not in managed[key]]
            if unmanaged:
                org.log.warn("Unmanaged %s in Organization: %s" %
                        (key,', '.join(unmanaged)))
                if key ==  'accounts':
                    # append unmanaged accounts to default_ou
                    place_unmanged_accounts(org, unmanaged)


if __name__ == "__main__":
    main()
