#!/usr/bin/env python3
# TODO: verify against live hardware -- this has NOT been run against a real
# NetworkManager or ConnMan instance. It was written by reading the real,
# current upstream D-Bus introspection XML / doc/*.txt for each project (see
# ../networkmanager/ and ../connman/, and README.md for citations), not from
# memory of either project's API, but the exact runtime behavior (property
# casing, error types, timing) needs confirming on a real box.
#
# Detects whether NetworkManager's or ConnMan's *real, already-shipped, not-yet-
# patched* D-Bus service is present on the system bus, and fetches current scan
# results using whatever each one ALREADY exposes today, reshaping the result
# into the common schema described in ./schema.json as best each backend allows.
#
# Once the NetworkManager and ConnMan patches described in README.md land, the
# backend-specific fetch functions below (`_fetch_networkmanager_apscan` and
# `_fetch_connman_apscan`) go away entirely and are replaced by one call to the
# new common interface, e.g.:
#
#     iface = await proxy.get_interface("org.freedesktop.WifiGeolocationScan1")
#     records = await iface.call_get_scan_results()
#
# against whichever backend's well-known bus name is present. Everything else
# in this file (bus-name detection, the ApScanRecord shape, __main__) stays.
#
# Library: dbus-next (async-native, no GLib mainloop dependency, works against
# both GDBus-based (NetworkManager) and libdbus-based (ConnMan) services).
#   pip install dbus-next

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any, Optional

from dbus_next.aio import MessageBus
from dbus_next import BusType, Variant

NM_BUS_NAME = "org.freedesktop.NetworkManager"
NM_OBJ_PATH = "/org/freedesktop/NetworkManager"
NM_MANAGER_IFACE = "org.freedesktop.NetworkManager"
NM_DEVICE_IFACE = "org.freedesktop.NetworkManager.Device"
NM_WIRELESS_IFACE = "org.freedesktop.NetworkManager.Device.Wireless"
NM_AP_IFACE = "org.freedesktop.NetworkManager.AccessPoint"
NM_PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"

# NMDeviceType enum value for Wi-Fi devices, from
# networkmanager/src/libnm-core-public/nm-dbus-interface.h:
#   NM_DEVICE_TYPE_WIFI = 2
NM_DEVICE_TYPE_WIFI = 2

CONNMAN_BUS_NAME = "net.connman"
CONNMAN_MANAGER_OBJ_PATH = "/"
CONNMAN_MANAGER_IFACE = "net.connman.Manager"
CONNMAN_SERVICE_IFACE = "net.connman.Service"
CONNMAN_TECHNOLOGY_IFACE = "net.connman.Technology"


@dataclasses.dataclass
class ApScanRecord:
    """Mirrors schema.json's per-record shape. See schema.json for the
    normative definition; this is a convenience Python mirror only."""

    macAddress: Optional[str]
    signalStrength: Optional[int]
    signalStrengthUnit: str  # "dBm" | "dBm_estimated_from_percent"
    source: str  # "networkmanager" | "connman"
    signalStrengthRawPercent: Optional[int] = None
    ssid: Optional[str] = None
    frequencyMhz: Optional[int] = None
    lastSeenMs: Optional[int] = None

    def as_dict(self) -> dict:
        d = dataclasses.asdict(self)
        # Match schema.json's additionalProperties:false expectations -- drop
        # None-valued optional fields rather than emit explicit nulls, since
        # some consumers may validate strictly against the schema's declared
        # (non-nullable) types for the optional fields.
        return {k: v for k, v in d.items() if v is not None or k in ("macAddress", "source")}


def _nm_percent_to_dbm_estimate(percent: int) -> int:
    """Best-effort percent->dBm estimate, per README.md's 'Design decision'
    section: inverts NetworkManager's own nm_wifi_utils_level_to_quality()
    formula (src/core/nm-core-utils.c), which linearly maps dBm in [-100, -40]
    onto percent in [0, 100]:

        percent = 100 - (100 * abs(clamp(dbm, -100, -40) + 40)) / 60

    Inverting for dbm (percent in [0, 100]):

        dbm = -40 - (60 * (100 - percent) / 100)

    This is explicitly an ESTIMATE -- NM's own source uses at least two OTHER,
    mutually-inconsistent dBm<->percent formulas elsewhere (nl80211 and WEXT
    driver paths, see README.md), and does not retain which formula produced
    a given AccessPoint.Strength value. Callers must treat the result as
    signalStrengthUnit="dBm_estimated_from_percent", never "dBm".
    """
    percent = max(0, min(100, percent))
    return round(-40 - (60 * (100 - percent) / 100))


def _connman_percent_to_dbm_estimate(percent: int) -> int:
    """Best-effort inverse of ConnMan's plugins/wifi.c calculate_strength():

        strength = min(100, 120 + signal_dbm)

    Inverting (and note the min(100, ...) clamp means percent==100 could
    correspond to any dbm >= -20, so this under-estimates strong signals):

        dbm = percent - 120

    Also an ESTIMATE for the same reasons as the NM case above.
    """
    percent = max(0, min(100, percent))
    return percent - 120


async def _detect_backend(bus: MessageBus) -> Optional[str]:
    """Returns 'networkmanager', 'connman', or None, by asking the bus which
    well-known service names currently have an owner. Prefers NetworkManager
    if (unusually) both are present, matching the stated priority in README.md."""
    dbus_obj = bus.get_proxy_object(
        "org.freedesktop.DBus", "/org/freedesktop/DBus",
        await bus.introspect("org.freedesktop.DBus", "/org/freedesktop/DBus"),
    )
    dbus_iface = dbus_obj.get_interface("org.freedesktop.DBus")

    for name, label in ((NM_BUS_NAME, "networkmanager"), (CONNMAN_BUS_NAME, "connman")):
        try:
            has_owner = await dbus_iface.call_name_has_owner(name)
        except Exception:
            has_owner = False
        if has_owner:
            return label
    return None


async def _fetch_networkmanager_apscan(bus: MessageBus) -> list[ApScanRecord]:
    """Uses NM's REAL, already-shipped D-Bus API today:
      - org.freedesktop.NetworkManager.GetAllDevices() / .Devices property
      - org.freedesktop.NetworkManager.Device.DeviceType property (==2 for wifi)
      - org.freedesktop.NetworkManager.Device.Wireless.GetAllAccessPoints()
      - org.freedesktop.NetworkManager.AccessPoint's properties (HwAddress,
        Strength, Ssid, Frequency, LastSeen)
    Confirmed against networkmanager/introspection/org.freedesktop.NetworkManager*.xml.

    NOTE: this does NOT call RequestScan() -- per README.md, triggering a scan
    is a separate, deliberate step left to the caller (watch the device's
    LastScan property via PropertiesChanged after calling RequestScan, THEN
    read access points). This function only reads whatever the device already
    has cached.
    """
    introspection = await bus.introspect(NM_BUS_NAME, NM_OBJ_PATH)
    manager_obj = bus.get_proxy_object(NM_BUS_NAME, NM_OBJ_PATH, introspection)
    manager_iface = manager_obj.get_interface(NM_MANAGER_IFACE)

    device_paths: list[str] = await manager_iface.call_get_all_devices()

    records: list[ApScanRecord] = []

    for dev_path in device_paths:
        dev_introspection = await bus.introspect(NM_BUS_NAME, dev_path)
        dev_obj = bus.get_proxy_object(NM_BUS_NAME, dev_path, dev_introspection)
        props_iface = dev_obj.get_interface(NM_PROPERTIES_IFACE)

        device_type = await props_iface.call_get(NM_DEVICE_IFACE, "DeviceType")
        if device_type.value != NM_DEVICE_TYPE_WIFI:
            continue

        wireless_iface = dev_obj.get_interface(NM_WIRELESS_IFACE)
        ap_paths: list[str] = await wireless_iface.call_get_all_access_points()

        for ap_path in ap_paths:
            ap_introspection = await bus.introspect(NM_BUS_NAME, ap_path)
            ap_obj = bus.get_proxy_object(NM_BUS_NAME, ap_path, ap_introspection)
            ap_props_iface = ap_obj.get_interface(NM_PROPERTIES_IFACE)

            all_props: dict[str, Variant] = await ap_props_iface.call_get_all(NM_AP_IFACE)

            hw_address: Optional[str] = all_props.get("HwAddress").value if "HwAddress" in all_props else None
            if not hw_address:
                # No BSSID reported for this AP object -- skip, macAddress is
                # required by schema.json.
                continue

            strength_percent = all_props.get("Strength").value if "Strength" in all_props else None
            frequency_mhz = all_props.get("Frequency").value if "Frequency" in all_props else None
            ssid_bytes = all_props.get("Ssid").value if "Ssid" in all_props else None

            ssid: Optional[str] = None
            if ssid_bytes:
                try:
                    ssid = bytes(ssid_bytes).decode("utf-8", errors="replace")
                except Exception:
                    ssid = None

            # LastSeen is CLOCK_BOOTTIME *seconds*, -1 == never seen -- per
            # networkmanager/introspection/org.freedesktop.NetworkManager.AccessPoint.xml.
            # Converting to an absolute epoch-ms timestamp requires knowing the
            # host's boottime/wallclock offset, which this skeleton does not
            # attempt -- left as None/omitted here; a real implementation of
            # the future common D-Bus interface should do this conversion
            # server-side (see README.md's lastSeenMs field description).
            last_seen_ms = None

            if strength_percent is None:
                # No usable signal info at all for this AP -- still required
                # by the schema, so skip rather than emit a fabricated value.
                continue

            records.append(
                ApScanRecord(
                    macAddress=str(hw_address).lower(),
                    signalStrength=_nm_percent_to_dbm_estimate(int(strength_percent)),
                    signalStrengthUnit="dBm_estimated_from_percent",
                    signalStrengthRawPercent=int(strength_percent),
                    ssid=ssid,
                    frequencyMhz=int(frequency_mhz) if frequency_mhz is not None else None,
                    lastSeenMs=last_seen_ms,
                    source="networkmanager",
                )
            )

    return records


async def _fetch_connman_apscan(bus: MessageBus) -> list[ApScanRecord]:
    """Uses ConnMan's REAL, already-shipped D-Bus API today:
      - net.connman.Manager.GetServices() -> array{object, dict}
      - net.connman.Service's "Strength" (uint8 percent) and "Type" properties

    IMPORTANT (see README.md 'ConnMan -- meaningfully larger patch'): as of the
    current upstream source examined for this project, ConnMan's D-Bus API has
    NO per-access-point BSSID anywhere -- net.connman.Service aggregates by
    SSID+security, not by physical AP/BSSID, and does not retain or expose a
    MAC address for the AP(s) backing a given service. This is confirmed by:
      - connman/doc/service-api.txt (full Service property list, no BSSID/MAC)
      - connman/src/network.c's `struct connman_network` (no bssid field)
      - connman/gsupplicant/gsupplicant.h (no g_supplicant_network_get_bssid(),
        even though the lower-level `struct g_supplicant_bss` in
        gsupplicant/supplicant.c DOES carry a bssid[6] -- it just never
        surfaces past that layer today)

    Until the ConnMan patch described in README.md lands, this function can
    only produce PARTIAL records: signal strength (as a percent-derived
    estimate) keyed by service identifier, with macAddress left as None. Per
    schema.json, macAddress is a REQUIRED field, so these partial records are
    NOT schema-valid as-is -- they are returned here anyway, clearly marked,
    for visibility into what ConnMan can and can't supply today. A real
    downstream consumer should treat ConnMan-sourced records as unusable for
    geolocation purposes until the patch lands.
    """
    introspection = await bus.introspect(CONNMAN_BUS_NAME, CONNMAN_MANAGER_OBJ_PATH)
    manager_obj = bus.get_proxy_object(CONNMAN_BUS_NAME, CONNMAN_MANAGER_OBJ_PATH, introspection)
    manager_iface = manager_obj.get_interface(CONNMAN_MANAGER_IFACE)

    # array{object,dict} GetServices() -- connman/doc/manager-api.txt
    services: list[tuple[str, dict[str, Variant]]] = await manager_iface.call_get_services()

    records: list[ApScanRecord] = []

    for service_path, props in services:
        service_type = props.get("Type")
        if service_type is None or service_type.value != "wifi":
            continue

        strength_variant = props.get("Strength")
        if strength_variant is None:
            continue
        strength_percent = int(strength_variant.value)

        name_variant = props.get("Name")
        ssid = name_variant.value if name_variant is not None else None

        records.append(
            ApScanRecord(
                # No BSSID available from ConnMan today -- see docstring above
                # and README.md. This makes the record schema-INVALID against
                # schema.json's required "macAddress"; surfaced anyway so
                # callers can see exactly what is/isn't available pre-patch.
                macAddress=None,
                signalStrength=_connman_percent_to_dbm_estimate(strength_percent),
                signalStrengthUnit="dBm_estimated_from_percent",
                signalStrengthRawPercent=strength_percent,
                ssid=ssid,
                frequencyMhz=None,  # not exposed by Service either, see README.md
                lastSeenMs=None,
                source="connman",
            )
        )

    return records


async def get_scan_results() -> list[ApScanRecord]:
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        backend = await _detect_backend(bus)
        if backend == "networkmanager":
            return await _fetch_networkmanager_apscan(bus)
        elif backend == "connman":
            return await _fetch_connman_apscan(bus)
        else:
            raise RuntimeError(
                "Neither org.freedesktop.NetworkManager nor net.connman has an "
                "owner on the system bus -- is a connection manager running?"
            )
    finally:
        bus.disconnect()


async def main() -> None:
    records = await get_scan_results()
    for r in records:
        print(r.as_dict())
    print(f"\n{len(records)} record(s)")


if __name__ == "__main__":
    asyncio.run(main())
