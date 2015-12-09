# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.


import pywsman
import re
from collections import namedtuple
from collections import OrderedDict
from wry import common
from wry.data_structures import WryDict, RadioButtons, EnablementMap
from wry import common
from wry import exceptions
from wry.config import RESOURCE_METHODS, RESOURCE_URIs, SCHEMAS



StateMap = namedtuple('StateMap', ['state', 'sub_state'])


AMT_KVM_ENABLEMENT_MAP = {
    2: StateMap(True, 'Enabled'),
    6: StateMap(True, 'Enabled But Offline'),
    3: StateMap(False, 'Disabled'),
}


AMT_POWER_STATE_MAP = [
    None,
    StateMap('other', None),
    StateMap('on', None),
    StateMap('sleep', 'Light'),
    StateMap('sleep', 'Deep'),
    StateMap('cycle', '(Off - Soft)'),
    StateMap('off', 'hard'),
    StateMap('hibernate', '(Off - Soft)'),
    StateMap('off', 'soft'),
    StateMap('cycle', '(Off - Hard)'),
    StateMap('Master Bus Reset', None),
    StateMap('Diagnostic Interrupt (NMI)', None),
    StateMap('off', 'Soft Graceful'),
    StateMap('off', 'Hard Graceful'),
    StateMap('Master Bus Reset', 'Graceful'),
    StateMap('cycle', '(Off - Soft Graceful)'),
    StateMap('cycle', '(Off - Hard Graceful)'),
    StateMap('Diagnostic Interrupt (INIT)', None),
]
'''
.. _CIM\_AssociatedPowerManagementService: http://software.intel.com/sites/manageability/AMT_Implementation_and_Reference_Guide/default.htm?turl=HTMLDocuments%2FWS-Management_Class_Reference%2FCIM_BootConfigSetting.htm

Mapping of device power states. A StateMap's index in this list, is the
PowerState value as specified in the CIM\_AssociatedPowerManagementService_
schema class.
'''


class lazy_property(object):
    '''A property that is evaluated on first access, and never again thereafter.'''

    def __init__(self, getter):
        self.getter = getter
        self.getter_name = getter.__name__

    def __get__(self, obj, _):
        if obj is None:
            return None
        value = self.getter(obj)
        setattr(obj, self.getter_name, value)
        return value


class AMTDevice(object):
    '''A wrapper class which packages AMT functionality into an accessible, device-centric format.'''

    def __init__(self, location, protocol, username, password):
        port = common.AMT_PROTOCOL_PORT_MAP[protocol]
        path = '/wsman'
        self.client = pywsman.Client(location, port, path, protocol, username, password)
        self.options = pywsman.ClientOptions()

        self.boot = AMTBoot(self.client, self.options)
        self.power = AMTPower(self.client, self.options)
        self.vnc = AMTKVM(self.client, self.options)
        self.opt_in = AMTOptIn(self.client, self.options)
        self.redirection = AMTRedirection(self.client, self.options)

    @property
    def debug(self):
        '''
        When set to True, openwsman will dump every [#]_ request made to the
        client.

        Unfortunately, openwsman does not expose this value, so it only possible
        to set this property, and not to retrieve it.

        .. [#] Actually, every request that makes use of self.options.
        '''
        raise NotImplemented('There is no way to get the value of this property. Please set it explicitly.')
        return self.options.get_dump_request()

    @debug.setter
    def debug(self, value):
        if value:
            self.options.set_dump_request()
        else:
            self.options.clear_dump_request()

    def get_resource(self, resource_name, as_xmldoc=False):
        '''
        Get a native representaiton of a resource, by name. The resource URI will be
        sourced from config.RESOURCE_URIs
        '''
        return common.get_resource(self.client, resource_name, options=self.options, as_xmldoc=as_xmldoc)

    def enumerate_resource(self, resource_name): # Add in all relevant kwargs...
        '''
        Get a native representaiton of a resource, and its instances. The
        resource URI will be sourced from config.RESOURCE_URIs
        '''
        return common.enumerate_resource(self.client, resource_name)

    def put_resource(self, data, uri=None, silent=False):
        '''
        Given a WryDict describing a resource, put this data to the client.
        '''
        return common.put_resource(self.client, data, uri, options=self.options, silent=silent)

    def dump(self, as_json=True):
        '''
        Print all of the known information about the device.

        :returns: WryDict or json.
        '''
        output = WryDict()
        impossible = []
        for name, methods in RESOURCE_METHODS.items():
            try:
                if 'get' in methods:
                    resource = self.get_resource(name)
                elif 'enumerate' in methods:
                    resource = self.enumerate_resource(name)
                else:
                    raise exceptions.NoSupportedMethods('The resource %r does not define a supported method for this action.' % name)
            except exceptions.WSManFault:
                impossible.append(name)
            else:
                output.update(resource)
        messages = ['# Could not dump %s' % name for name in impossible]
        if as_json:
            return '\n'.join(messages) + '\n' + output.as_json()
        else:
            print '\n'.join(messages)
            return output

    def load(self, input_dict):
        return common.load_from_dict(client, input_dict)


class DeviceCapability(object):
    '''self.resource_name should be set on the subclass if needed.'''

    def __init__(self, client, options=None):
        self.client = client
        self.options = options

    def get(self, resource_name=None, setting=None):
        if not resource_name:
            resource_name = self.resource_name
        resource = common.get_resource(self.client, resource_name, options=self.options)
        if setting:
            return resource[resource_name][setting]
        return resource[resource_name]

    def put(self, resource_name=None, input_dict=None, silent=False,
        as_update=True): # Ideally want keyword-only args or a refactor here.
                         # Want to be able to supply only input_dict...
        if not resource_name:
            resource_name = self.resource_name
        if as_update:
            resource = common.get_resource(self.client, resource_name, options=self.options)
            resource[resource_name].update(input_dict)
        else:
            resource = WryDict({resource_name: input_dict})
        response = common.put_resource(self.client, resource, silent=silent, options=self.options)

    def walk(self, resource_name,  wsman_filter=None):
        '''Enumerate a resource.'''
        return common.enumerate_resource(self.client, resource_name, wsman_filter=wsman_filter, options=self.options)

class AMTPower(DeviceCapability):
    '''Control over a device's power state.'''

    def __init__(self, *args, **kwargs):
        self.resource_name = 'CIM_AssociatedPowerManagementService'
        super(AMTPower, self).__init__(*args, **kwargs)

    def request_power_state_change(self, power_state): 
        return common.invoke_method(
            service_name='CIM_PowerManagementService',
            resource_name='CIM_ComputerSystem',
            affected_item='ManagedElement',
            method_name='RequestPowerStateChange',
            options=self.options,
            client=self.client,
            selector=('Name', 'ManagedSystem', 'Intel(r) AMT Power Management Service', ),
            args_before=[('PowerState', str(power_state)), ],
            anonymous=True,
        )

    @property
    def state(self):
        '''
        A property which describes the machine's power state.
        
        A :class:`wry.device.StateMap` as described in
        :data:`wry.device.AMT_POWER_STATE_MAP`.
        '''
        response = self.get(setting='PowerState')
        return AMT_POWER_STATE_MAP[response]

    def turn_on(self):
        '''Turn on the device.'''
        sub_state = None
        index = AMT_POWER_STATE_MAP.index(('on', sub_state))
        self.request_power_state_change(index)

    def turn_off(self):
        '''Turn off the device.'''
        return self.request_power_state_change(8)

    def reset(self):
        '''Reboot the device.'''
        return self.request_power_state_change(5)

    def toggle(self):
        """
        If the device is off, turn it on.
        If it is on, turn it off.
        """
        state = self.state
        if state == 'on':
            self.turn_off()
        elif state == 'off':
            self.turn_on()
        else:
            raise SomeError


class AMTKVM(DeviceCapability):
    '''Control over a device's KVM (VNC) functionality.'''

    def request_state_change(self, resource_name, requested_state):
        input_dict = {
            resource_name:
                {'RequestStateChange_INPUT': {
                    'RequestedState': requested_state,
                },
            }
        }
        return common.invoke_method(
            service_name='CIM_KVMRedirectionSAP',
            method_name='RequestStateChange',
            options=self.options,
            client=self.client,
            args_before=[('RequestedState', str(requested_state)), ],
        )

    @property
    def enabled(self):
        '''
        Whether KVM functionality is enabled or disabled.

        True/False

        .. note:: This will return True even if KVM is enabled, but no ports for it
           are.
        '''
        e_state = self.get('CIM_KVMRedirectionSAP', 'EnabledState')
        return AMT_KVM_ENABLEMENT_MAP[e_state].state

    @enabled.setter
    def enabled(self, value):
        if value is True:
            self.request_state_change('CIM_KVMRedirectionSAP', 2)
        elif value is False:
            self.request_state_change('CIM_KVMRedirectionSAP', 3)
        else:
            raise TypeError('Please specify Either True or False.')

    @property
    def enabled_ports(self):
        '''Tells you (and/or allows you to set) the enabled ports for VNC.'''

        def iadd(values):
            self.enabled_ports = self.enabled_ports.enabled + values

        def isub(values):
            self.enabled_ports = set(self.enabled_ports.values) - set(values)

        ports = EnablementMap(5900, 16994, 16995, iadd=iadd, isub=isub)

        if self.get('IPS_KVMRedirectionSettingData', 'Is5900PortEnabled'):
            ports.toggle(5900)
        if self.get('AMT_RedirectionService', 'ListenerEnabled'):
            ports.toggle(16994)
            if self.walk('AMT_TLSSettingData')['AMT_TLSSettingData'][0]['Enabled']:
                ports.toggle(16995)
        return ports

    @enabled_ports.setter
    def enabled_ports(self, values):
        ports = self.enabled_ports.values
        enabled = self.enabled_ports.enabled
        print 'values: ', values
        print 'ports: ', ports
        # Validation:
        invalid = list(set(values) - set(ports))
        if invalid:
            raise ValueError('Invalid port(s) specified: %r. Valid ports are %r.'
                % (invalid, ports))
        if 16995 in values and 16995 not in enabled:
            if 16994 not in values:
                raise ValueError('Port 16995 cannot be enabled unless port 16994 is enabled also.')
            else:
                if not self.walk('AMT_TLSSettingData')['AMT_TLSSettingData'][0]['Enabled']:
                    raise ValueError('Port 16995 can only be set by enabling both TLS and port 16994.')
        # Setter logic:
        for port, enable in [(port, port in values) for port in ports]:
            if (enable and port not in enabled) or (not enable and port in enabled):
                if port == 5900:
                    self.put('IPS_KVMRedirectionSettingData', {'Is5900PortEnabled': enable})
                elif port == 16994:
                    self.put('AMT_RedirectionService', {'ListenerEnabled': enable})
                self.enabled_ports.toggle(port)
        #return self.enabled_ports

    @property
    def default_screen(self):
        ''' Default Screen. An integer.'''
        return self.get('IPS_KVMRedirectionSettingData', 'DefaultScreen')

    @default_screen.setter
    def default_screen(self, value):
         self.put('IPS_KVMRedirectionSettingData', {'DefaultScreen': value})

    @property
    def opt_in_timeout(self):
        '''
        User opt-in timeout for KVM access, in seconds.

        If set to 0, opt-in will be disabled.
        '''
        timeout = (not self.get('IPS_KVMRedirectionSettingData', 'OptInPolicy')) or self.get('IPS_KVMRedirectionSettingData', 'OptInPolicyTimeout')
        if timeout is True:
            return 0
        return timeout

    @opt_in_timeout.setter
    def opt_in_timeout(self, value):
        if not value:
             self.put('IPS_KVMRedirectionSettingData', {'OptInPolicy': False})
        else:
             self.put('IPS_KVMRedirectionSettingData', {'OptInPolicy': True, 'OptInPolicyTimeout': value})

    @property
    def session_timeout(self):
        '''
        Session timeout. In minutes.
        '''
        return self.get('IPS_KVMRedirectionSettingData', 'SessionTimeout')

    @session_timeout.setter
    def session_timeout(self, value):
        self.put({'SessionTimeout': value})

    @property
    def password(self):
        raise AttributeError('This is a write-only attribute.')

    @password.setter
    def password(self, value):
        self.put('IPS_KVMRedirectionSettingData'), {'RFBPassword': value}


class AMTRedirection(DeviceCapability):
    '''Control over Serial-over-LAN and storage redirection.'''

    def __init__(self, *args, **kwargs):
        self._state_mapping = OrderedDict([
            (0, 'Unknown'),
            (1, 'Other'),
            (2, 'Enabled'),
            (3, 'Disabled'),
            (4, 'Shutting Down'),
            (5, 'Not Applicable'),
            (6, 'Enabled but Offline'),
            (7, 'In Test'),
            (8, 'Deferred'),
            (9, 'Quiesce'),
            (10, 'Starting'),
            (11, 'DMTF Reserved'),
            (32768, 'IDER and SOL are disabled'),
            (32769, 'IDER is enabled and SOL is disabled'),
            (32770, 'SOL is enabled and IDER is disabled'),
            (32771, 'IDER and SOL are enabled'),
        ])
        super(AMTRedirection, self).__init__(*args, **kwargs)
    
    @property
    def enabled_features(self):
        items = EnablementMap('SoL', 'IDER')
        state = self.get('AMT_RedirectionService', 'EnabledState')
        if state >= 32768:
            if state in (32769, 32771):
                items.toggle('IDER')
            if state in (32770, 32771):
                items.toggle('SoL')
        else:
            if state in self._state_mapping:
                raise LookupError('Unknown state discovered: %r' % self._state_mapping[state])
            raise KeyError('Unknown state discovered: %r' % state)
        return items

    @enabled_features.setter
    def enabled_features(self, features):
        if not features:
            self.put('AMT_RedirectionService', {'EnabledState': 32768})
        elif set(features) == set(['SoL', 'IDER']):
            self.put('AMT_RedirectionService', {'EnabledState': 32771})
        elif features[0] == 'SoL':
            self.put('AMT_RedirectionService', {'EnabledState': 32770})
        elif features[0] == 'IDER':
            self.put('AMT_RedirectionService', {'EnabledState': 32769})
        else:
            raise ValueError('Invalid data provided. Please provide a list comprising only of the following elements: %s' % ', '.join([value.__repr__ for value in self.enabled.values])


class AMTOptIn(DeviceCapability):
    '''Manage user consent and opt-in codes.'''

    def __init__(self, *args, **kwargs):
        self._consent_mapping = OrderedDict([
            (0, None),
            (1, 'KVM'),
            (4294967295, 'All'),
        ])
        self._consent_values = RadioButtons(self._consent_mapping.values())
        super(AMTOptIn, self).__init__(*args, **kwargs)

    @property
    def required(self):
        level = self._consent_mapping[self.get('IPS_OptInService', 'OptInRequired')]
        self._consent_values.selected = level
        return self._consent_values

    @required.setter
    def required(self, value):
        for key, val in self._consent_mapping.items():
            if value == val:
                break
        else:
            raise KeyError
        self.put('IPS_OptInService', {'OptInRequired': key})
        self._consent_values.selected = value

    @property
    def code_ttl(self):
        '''How long an opt-in code lasts, in seconds.'''
        return self.get('IPS_OptInService', 'OptInCodeTimeout')

    @code_ttl.setter
    def code_ttl(self, value):
        try:
            assert type(value) == int
            assert 60 <= value <= 900
        except (TypeError, AssertionError):
            raise TypeError('TTL (in seconds) must be an integer between 60 and 900.')
        self.put('IPS_OptInService', {'OptInCodeTimeout': value})

    @property
    def state(self):
        mapping = {
            0: 'Not started',
            1: 'Requested',
            2: 'Displayed',
            3: 'Received',
            4: 'In Session',
        }
        return mapping[self.get('IPS_OptInService', 'OptInState')]


class AMTBoot(DeviceCapability):
    '''Control how the machine will boot next time.'''

    @property
    def supported_media(self):
        '''Media the device can be configured to boot from.'''
        returned = self.walk('CIM_BootSourceSetting')
        return [source['StructuredBootString'].split(':')[-2] for source in returned['CIM_BootSourceSetting']]

    @property
    def medium(self):
        raise NotImplemented('It is not currently possible to detect which medium a device will boot from.')

    @medium.setter
    def medium(self, value):
        '''Set boot medium for next boot.'''
        # Zero out boot options - unwise, but just testing right now...
        settings = self.get('AMT_BootSettingData')
        for setting in settings:
            if type(settings[setting]) == int:
                settings[setting] = 0
            elif type(settings[setting]) == bool:
                settings[setting] = False
            else:
                pass

        sources = self.walk('CIM_BootSourceSetting')['CIM_BootSourceSetting']
        for source in sources:
            if value in source['StructuredBootString']:
                instance_id = source['InstanceID']
                break
        else:
            raise LookupError('This medium is not supported by the device')

        boot_config = self.get('CIM_BootConfigSetting') # Should be an
        # enumerate, as it has intances... But for now...
        config_instance = str(boot_config['InstanceID'])

        response = common.invoke_method(
            service_name='CIM_BootConfigSetting',
            resource_name='CIM_BootSourceSetting',
            affected_item='Source',
            method_name='ChangeBootOrder',
            options=self.options,
            client=self.client,
            selector=('InstanceID', instance_id, config_instance, ),
        )
        self._set_boot_config_role()

    @property
    def config(self):
        '''Get configuration for the machine's next boot.'''
        return self.get('AMT_BootSettingData')

    def _set_boot_config_role(self, enabled_state=True):
        if enabled_state == True:
            role = '1'
        elif enabled_state == False:
            role = '32768'
        svc = self.get('CIM_BootService')
        assert svc['ElementName'] == 'Intel(r) AMT Boot Service'
        return common.invoke_method(
            service_name='CIM_BootService',
            resource_name='CIM_BootConfigSetting',
            affected_item='BootConfigSetting',
            method_name='SetBootConfigRole',
            options=self.options,
            client=self.client,
            selector=('InstanceID', 'Intel(r) AMT: Boot Configuration 0', ),
            args_after=[('Role', role)],
        )

