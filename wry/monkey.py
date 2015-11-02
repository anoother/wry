'''Monkey-patching pywsman for fun and profit.'''

import pywsman



def copy_client_options(self):
    new_options = pywsman.ClientOptions()
    for attr in dir(self):
        if attr.startswith('get_'):
            setter = attr.replace('get_', 'set_')
            value = getattr(self, attr)()
            getattr(self, setter)(value)
    return new_options


pywsman.ClientOptions.__copy__ = copy_client_options

