"""hooks/guards -- package marker only.

Deliberately empty: importing this package must not import any guard module (guards
are resolved and imported individually, by id, in dispatch.py) and must not import
psutil transitively (I-13.2). Do not add imports here.
"""
