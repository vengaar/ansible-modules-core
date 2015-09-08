#!/usr/bin/python
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

DOCUMENTATION = """
---
module: ec2_elb
short_description: De-registers or registers instances from EC2 ELBs
description:
  - This module de-registers or registers an AWS EC2 instance(s) from the ELBs
    that it belongs to.
  - Returns fact "ec2_elbs" which is a list of elbs attached to the instance(s)
    if state=absent is passed as an argument.
  - Will be marked changed when called only if there are ELBs found to operate on.
version_added: "1.2"
author: "John Jarvis (@jarv)"
options:
  state:
    description:
      - register or deregister the instance(s)
    required: true
    choices: ['present', 'absent']
  instance_ids:
    description:
      - list of EC2 Instance ID (alias of instance_id)
    required: true
    aliases: ["instance_id"]    
  ec2_elbs:
    description:
      - List of ELB names, required for registration. The ec2_elbs fact should be used if there was a previous de-register.
    required: false
    default: None
  region:
    description:
      - The AWS region to use. If not specified then the value of the EC2_REGION environment variable, if any, is used.
    required: false
    aliases: ['aws_region', 'ec2_region']
  enable_availability_zone:
    description:
      - Whether to enable the availability zone of the instance(s) on the target ELB if the availability zone has not already
        been enabled. If set to no, the task will fail if the availability zone is not enabled on the ELB.
    required: false
    default: yes
    choices: [ "yes", "no" ]
  wait:
    description:
      - Wait for instance(s) registration or deregistration to complete successfully before returning.  
    required: false
    default: yes
    choices: [ "yes", "no" ] 
  validate_certs:
    description:
      - When set to "no", SSL certificates will not be validated for boto versions >= 2.6.0.
    required: false
    default: "yes"
    choices: ["yes", "no"]
    aliases: []
    version_added: "1.5"
  wait_timeout:
    description:
      - Number of seconds to wait for an instance(s) to change state. If 0 then this module may return an error if a transient error occurs. If non-zero then any transient errors are ignored until the timeout is reached. Ignored when wait=no.
    required: false
    default: 0
    version_added: "1.6"
extends_documentation_fragment: aws
"""

EXAMPLES = """
# basic pre_task and post_task example
pre_tasks:
  - name: Gathering ec2 facts
    action: ec2_facts
  - name: Instance De-register
    local_action:
      module: ec2_elb
      instance_id: "{{ ansible_ec2_instance_id }}"
      state: 'absent'
roles:
  - myrole
post_tasks:
  - name: Instance Register
    local_action: 
      module: ec2_elb
      instance_id: "{{ ansible_ec2_instance_id }}"
      ec2_elbs: "{{ item }}"
      state: 'present'
    with_items: ec2_elbs
"""

RETURN = '''
ansible_facts:
    description: a dict with the key "ec2_elbs" with as value the list names of lb managed.
    returned: success
    type: dict
    sample: "ec2_elbs": ["lb-name1", "lb-name2", "lb-name3"]}
elb_changed:
    description: a dict with lb name has key and for each name the list of instance_id updated.
    returned: success
    type: dict
    sample: { "lb-name1": ["i-12345678","i-34567890"], "lb-name2": [], "lb-name3": ["i-12345678"] }
wait_attempt:
    description: a dict with lb name has key and for each name the number of attempt to have expected state for all these instances. Make sense only with wait true. 
    returned: success
    type: dict
    sample: { "lb-name1": 4, "lb-name2": 0, "lb-name3": 1 }
'''

import time
import sys
import os
import copy

try:
    import boto
    import boto.ec2
    import boto.ec2.elb
    from boto.regioninfo import RegionInfo
    HAS_BOTO = True
except ImportError:
    HAS_BOTO = False    

class ElbManager:
    """Handles EC2 instance ELB registration and de-registration"""

    def __init__(self, module, instance_ids=None, ec2_elbs=None,
                 region=None, **aws_connect_params):
        self.module = module
        self.instance_ids = instance_ids
        self.region = region
        self.aws_connect_params = aws_connect_params
        self.lbs = self._get_instance_lbs(ec2_elbs)
        self.changed = False
        self.elb_changed = dict()
        self.wait_attempt = dict()

    def deregister(self, wait, timeout):
        """De-register the instance from all ELBs and wait for the ELB
        to report it out-of-service"""


        initial_states = dict()
        for lb in self.lbs:
            initial_states[lb.name] = self._get_instance_health(lb) 
            lb.deregister_instances(self.instance_ids)

        if wait:
            for lb in self.lbs:
                self._await_elb_instance_state(lb, 'OutOfService', initial_states[lb.name], timeout)
        else:
            # We cannot assume no change was made if we don't wait
            # to find out
            self.changed = True                

    def register(self, wait, enable_availability_zone, timeout):
        """Register the instance for all ELBs and wait for the ELB
        to report the instance in-service"""
        
        initial_states = dict()
        for lb in self.lbs:
            initial_states[lb.name] = self._get_instance_health(lb)            
            if enable_availability_zone:
                self._enable_availailability_zone(lb)            
            lb.register_instances(self.instance_ids)            
        if wait:
            for lb in self.lbs:
                self._await_elb_instance_state(lb, 'InService', initial_states[lb.name], timeout)
        else:
            # We cannot assume no change was made if we don't wait
            # to find out
            self.changed = True

    def exists(self, lbtest):
        """ Verify that the named ELB actually exists """

        found = False
        for lb in self.lbs:
            if lb.name == lbtest:
                found=True
                break
        return found

    def _enable_availailability_zone(self, lb):
        """Enable the current instance's availability zone in the provided lb.
        lb: load balancer"""
        instances = self._get_instance()
        for instance in instances:
            if instance.placement not in lb.availability_zones:
                lb.enable_zones(zones=instance.placement)

    def _await_elb_instance_state(self, lb, awaited_state, initial_states, timeout):
        """Wait for an ELB to change state
        lb: load balancer
        awaited_state : state to poll for (string)"""

        self.elb_changed[lb.name] = []
        self.wait_attempt[lb.name] = 0
        max_time = time.time() + timeout
        while True:
            instance_states = self._get_instance_health(lb)
            instances_not_ok = copy.deepcopy(self.instance_ids) 
            for instance_id,instance_state in instance_states.iteritems():
                if instance_state.state == awaited_state:
                    instances_not_ok.remove(instance_id)                    
                    # Check the current state against the initial state, and only set
                    # changed if they are different.
                    if instance_state.state != initial_states[instance_id].state:
                        self.changed = True
                        self.elb_changed[lb.name].append(instance_id)

            if len(instances_not_ok) == 0:
                break
            
            if time.time() >= max_time:
                msg = "Timeout exceeded when waiting states instances of LB {0}".format(
                    lb.name
                )
                self.module.fail_json(
                    msg=msg,
                    wait_attempt=self.wait_attempt
                )
            
            self.wait_attempt[lb.name] = self.wait_attempt[lb.name] + 1
            # min healthcheck interval
            time.sleep(lb.health_check.interval)


    def _get_instance_health(self, lb):
        """
        Check instance health, should return status object or None under
        certain error conditions.
        """
        try:
            instancestate_list = lb.get_instance_health(self.instance_ids)
            instance_states = dict(
                 (instance_state.instance_id,instance_state)
                 for instance_state in instancestate_list  
            )
        except boto.exception.BotoServerError, e:
            if e.error_code == 'InvalidInstance':
                self.module.fail_json(msg=str(e))
        return instance_states

    def _get_instance_lbs(self, ec2_elbs=None):
        """Returns a list of ELBs attached to self.instance_ids
        ec2_elbs: an optional list of elb names that will be used
                  for elb lookup instead of returning what elbs
                  are attached to self.instance_ids"""

        try:
            elb = connect_to_aws(boto.ec2.elb, self.region, 
                                 **self.aws_connect_params)
        except (boto.exception.NoAuthHandlerFound, StandardError), e:
            self.module.fail_json(msg=str(e))

        elbs = elb.get_all_load_balancers()

        if ec2_elbs:
            lbs = sorted(lb for lb in elbs if lb.name in ec2_elbs)
        else:
            lbs = []
            instances_searched = set(self.instance_ids)
            for lb in elbs:
                instances_lb = set([
                    info.id
                    for info in lb.instances
                ])
                if instances_searched.issubset(instances_lb):
                    lbs.append(lb)

        return lbs

    def _get_instance(self):
        """Returns a boto.ec2.InstanceObject for self.instance_ids"""
        try:
            ec2 = connect_to_aws(boto.ec2, self.region, 
                                 **self.aws_connect_params)
        except (boto.exception.NoAuthHandlerFound, StandardError), e:
            self.module.fail_json(msg=str(e))
        only_instances = ec2.get_only_instances(instance_ids=self.instance_ids)
        return only_instances


def main():
    argument_spec = ec2_argument_spec()
    argument_spec.update(dict(
            state={'required': True},
            instance_ids={'required': True, 'type':'list', 'aliases':["instance_id"] },
            ec2_elbs={'default': None, 'required': False, 'type':'list'},
            enable_availability_zone={'default': True, 'required': False, 'type': 'bool'},
            wait={'required': False, 'default': True, 'type': 'bool'},
            wait_timeout={'required': False, 'default': 0, 'type': 'int'}
        )
    )

    module = AnsibleModule(
        argument_spec=argument_spec,
    )
    if not HAS_BOTO:
        module.fail_json(msg='boto required for this module')

    region, ec2_url, aws_connect_params = get_aws_connection_info(module)

    if not region: 
        module.fail_json(msg="Region must be specified as a parameter, in EC2_REGION or AWS_REGION environment variables or in boto configuration file")

    ec2_elbs = module.params['ec2_elbs']
    wait = module.params['wait']
    enable_availability_zone = module.params['enable_availability_zone']
    timeout = module.params['wait_timeout']

    if module.params['state'] == 'present' and 'ec2_elbs' not in module.params:
        module.fail_json(msg="ELBs are required for registration")

    instance_ids = module.params['instance_ids']
    
    elb_man = ElbManager(module, instance_ids, ec2_elbs, 
                         region=region, **aws_connect_params)

    if ec2_elbs is not None:
        for elb in ec2_elbs:
            if not elb_man.exists(elb):
                msg="ELB %s does not exist" % elb
                module.fail_json(msg=msg)

    if module.params['state'] == 'present':
        elb_man.register(wait, enable_availability_zone, timeout)
    elif module.params['state'] == 'absent':
        elb_man.deregister(wait, timeout)

    ansible_facts = {'ec2_elbs': [lb.name for lb in elb_man.lbs]}
    ec2_facts_result = dict(
        changed=elb_man.changed,
        elb_changed=elb_man.elb_changed,
        ansible_facts=ansible_facts,
        wait_attempt=elb_man.wait_attempt,
    )

    module.exit_json(**ec2_facts_result)

# import module snippets
from ansible.module_utils.basic import *
from ansible.module_utils.ec2 import *

main()
