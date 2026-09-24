"""Builders for fake /sys trees with real symlinks, shaped like this kernel's."""

from __future__ import annotations

import os
from pathlib import Path


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")


def _link(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(os.path.relpath(target, link.parent), link)


def _drivers(sys_root: Path, bus: str, name: str) -> Path:
    path = sys_root / "bus" / bus / "drivers" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def _netdev(sys_root: Path, iface: str, devnode: Path, perm_mac: str, mac: str | None = None,
            operstate: str = "up") -> Path:
    net = devnode / "net" / iface
    _write(net / "address", mac or perm_mac)
    _write(net / "operstate", operstate)
    _write(net / "phy80211" / "macaddress", perm_mac)
    _write(net / "statistics" / "rx_bytes", "0")
    _write(net / "statistics" / "tx_bytes", "0")
    _link(net / "device", devnode)
    _link(sys_root / "class" / "net" / iface, net)
    return net


def add_pci_radio(sys_root: Path, iface: str = "wifi0", addr: str = "0000:01:00.0",
                  vendor: str = "0x14c3", device: str = "0x7925", driver: str = "mt7925e",
                  perm_mac: str = "3c:3b:ad:16:b7:30", bridge: str = "0000:00:02.2") -> Path:
    devnode = sys_root / "devices" / "pci0000:00" / bridge / addr
    _write(devnode / "vendor", vendor)
    _write(devnode / "device", device)
    _write(devnode / "class", "0x028000")
    _link(devnode / "subsystem", sys_root / "bus" / "pci")
    if driver:
        _link(devnode / "driver", _drivers(sys_root, "pci", driver))
    _link(sys_root / "bus" / "pci" / "devices" / addr, devnode)
    if iface:
        _netdev(sys_root, iface, devnode, perm_mac)
    return devnode


def add_usb_radio(sys_root: Path, iface: str, bus_num: int, port: int, speed: int,
                  vendor: str, product: str, driver: str, perm_mac: str,
                  peer: bool = True, hub_port: int | None = None) -> Path:
    """A USB wifi stick on a root-hub port, or behind an external hub.

    Mirrors e.g. usb7/7-1/7-1:1.0/net/wifi2 with the port node at
    usb7/7-0:1.0/usb7-port1 (root hub) or 7-1/7-1:1.0/7-1-port2 (hub).
    """
    usb = sys_root / "devices" / "pci0000:00" / "0000:00:08.3" / "0000:05:00.4" / f"usb{bus_num}"
    _write(usb / "speed", "480")
    root_port = usb / f"{bus_num}-0:1.0" / f"usb{bus_num}-port{port}"
    root_port.mkdir(parents=True, exist_ok=True)
    if hub_port is None:
        udev = usb / f"{bus_num}-{port}"
        port_node = root_port
    else:
        hub = usb / f"{bus_num}-{port}"
        _write(hub / "speed", "480")
        _link(root_port / "device", hub)
        udev = hub / f"{bus_num}-{port}.{hub_port}"
        port_node = hub / f"{bus_num}-{port}:1.0" / f"{bus_num}-{port}-port{hub_port}"
        port_node.mkdir(parents=True, exist_ok=True)
    _write(udev / "speed", str(speed))
    _write(udev / "idVendor", vendor)
    _write(udev / "idProduct", product)
    _link(port_node / "device", udev)
    if peer:
        other = sys_root / "devices" / "peer-bus" / "usbX-port1"
        other.mkdir(parents=True, exist_ok=True)
        _link(port_node / "peer", other)
    intf = udev / f"{udev.name}:1.0"
    intf.mkdir(parents=True, exist_ok=True)
    _link(intf / "subsystem", sys_root / "bus" / "usb")
    _link(intf / "driver", _drivers(sys_root, "usb", driver))
    _netdev(sys_root, iface, intf, perm_mac)
    return udev
