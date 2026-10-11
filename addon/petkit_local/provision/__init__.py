"""Pointing a device at this server without Bluetooth: the telnet path.

The Axera generation (YumShare Dual-Hopper 2, "D4SH3") ships with a root
telnet login and keeps its server addresses in `/opt/user.conf`, encrypted
with a key that sits in its own `ble` binary. `userconf.py` reads and writes
that file, `telnet.py` is the shell it is fetched and written through, and
`telnet_flow.py` is the sequence the Provision tab runs: read, decrypt, point
`serverInfo` at this server, encrypt, write back under a verified checksum,
reboot. Everything the device had is kept as a backup, on the device and
under the add-on's data directory.
"""
