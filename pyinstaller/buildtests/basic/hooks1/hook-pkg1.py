attrs = [('notamodule','')]
def hook(mod):
    import os, sys, importlib.util
    other = os.path.join(mod.__path__[0], '../pkg2/__init__.pyc')
    if os.path.exists(other):
        spec = importlib.util.spec_from_file_location(mod.__name__, other)
        co = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(co)
    else:
        co = compile(open(other[:-1],'rU').read()+'\n', other, 'exec')
    mod.__init__(mod.__name__, other, co)
    mod.__path__.append(os.path.join(mod.__path__[0], 'extra'))
    return mod
