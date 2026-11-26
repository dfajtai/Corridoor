"""Corridoor: an OpenVPN split tunnel to one host, then SSH or HopToDesk.

The package is layered; every module only imports from the ones above it:

``errors``      exceptions shared by all modules
``winapi``      the only code that talks to Windows (DPAPI, jobs, mutexes)
``hoptodesk``   HopToDesk constants and the parser of the remote probe
``settings``    the ``Settings`` dataclass
``profile``     pure text handling of ``.ovpn`` profiles
``net``         small network probes (ping, routing table lookup)
``pipes``       one-way named pipes that hand secrets to child processes
``store``       the encrypted, administrator-only folder on disk
``session``     one connection attempt: the steps and the child processes
``diagnose``    the full diagnostics run (uses ``session``)
``pipetest``    the named pipe self-test (uses ``session``)
``gui``         the Tkinter windows

See ``docs/architecture.md`` for the details.
"""
