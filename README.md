# dbus-wifi-scan-schema

A reconciled, backend-agnostic shape for "list of nearby Wi-Fi access points with
BSSID + signal strength" over D-Bus, for feeding Wi-Fi-based geolocation APIs
(Google Geolocation API / Mozilla Ichnaea) from a Home Assistant OS box, regardless
of whether that box is running NetworkManager or ConnMan as its connection manager.

This is the design doc only. The two follow-on efforts (out of scope for this repo,
tracked separately) are:

- `../networkmanager/` -- a future patch to NetworkManager adding a D-Bus interface
  that reports this shape natively (NM is the priority target: Home Assistant Green,
  the primary hardware target, runs NetworkManager).
- `../connman/` -- a future patch to ConnMan doing the same (secondary priority; used
  by other HAOS hardware/images).

Both `networkmanager/` and `connman/` in this repo currently contain **real, current,
shallow clones of each project's actual upstream source** (NetworkManager from
`gitlab.freedesktop.org/NetworkManager/NetworkManager`, depth 1, HEAD at commit
`723dbb2f`; ConnMan from `git.kernel.org/pub/scm/network/connman/connman.git`, depth 1,
HEAD at commit `9e46e4a0`), used only as research material for this document. All
findings below are cited against that source, not against pre-existing/trained
knowledge of either project's API -- both projects have evolved for years and that
memory is exactly what this exercise was designed to not trust.

## Proposed common D-Bus interface

```
Interface:   org.freedesktop.WifiGeolocationScan1
Object path: implementation-defined (e.g. /org/freedesktop/WifiGeolocationScan1,
             or hung off the existing NM/ConnMan wifi device/technology object --
             an implementation detail for the patch authors, not fixed here)
```

Name is a strawman: `org.freedesktop.*` is really freedesktop.org's namespace and
neither NM's nor ConnMan's upstream would necessarily accept a patch registering an
interface under it without discussion. It's used here because both projects already
publish under `org.freedesktop.NetworkManager.*` / `net.connman.*` respectively and a
vendor-neutral, clearly-new name in the same rough style is easiest to reason about
in a strawman doc. If/when this becomes a real patch proposal, expect upstream
maintainers to want something more like `org.freedesktop.NetworkManager.Device.WifiGeolocation1`
on the NM side and `net.connman.Technology.WifiGeolocation1` (or similar,
namespaced under each project's own prefix) on the ConnMan side, rather than one
identical interface name registered on both buses' well-known service names. That's
a call for the actual patch authors; this doc fixes the *payload shape*, not the
bikeshed.

### Methods

```
array{dict} GetScanResults()
```

Returns the most recently completed scan's results, in the shape described below
(one dict per access point). No arguments. This is a **read of cached results
only** -- see next point for why there is deliberately no "trigger a scan and wait"
method on this interface.

**No new scan-triggering method is proposed.** Both backends already have a
perfectly good native one, and re-implementing scan-triggering isn't the point of
this contract:

- NetworkManager: `org.freedesktop.NetworkManager.Device.Wireless.RequestScan(a{sv} options)`
  (`../networkmanager/introspection/org.freedesktop.NetworkManager.Device.Wireless.xml`).
  Caller should watch the device's `LastScan` property (CLOCK_BOOTTIME ms) via
  `org.freedesktop.DBus.Properties.PropertiesChanged` to know when a triggered scan
  has actually completed, per NM's own doc comment on `RequestScan`.
- ConnMan: `net.connman.Technology.Scan()` (`../connman/doc/technology-api.txt`).
  Per ConnMan's own doc comment, this call **blocks until the scan finishes** ("The
  method call will return when a scan has been finished and results are available
  ... setting a longer D-Bus timeout might be a really good idea"), which is a
  materially different calling convention from NM's fire-and-then-watch-a-property
  pattern. A downstream client that wants one consistent "trigger + wait" experience
  across both backends will need to paper over that difference itself (e.g. always
  call the native trigger, then always poll/wait on `GetScanResults()`); that's a
  client-side concern, not something this D-Bus contract should try to normalize by
  adding a third method that reimplements scan-triggering.

A downstream client's expected flow is therefore: call the backend's *native*
scan-trigger method (detect which backend is present per the reference client),
wait for it to settle (property-watch for NM, natural blocking return for ConnMan),
then call `GetScanResults()` on this new common interface to get results back in
one consistent shape.

## Reconciled response shape

See `schema.json` (JSON Schema draft 2020-12) for the authoritative, machine-readable
per-record shape; this section explains the reasoning.

Each record has, at minimum:

| Field | Type | Notes |
|---|---|---|
| `macAddress` | string | BSSID, `aa:bb:cc:dd:ee:ff` lower-case colon-hex. Required. |
| `signalStrength` | integer | dBm (see conversion discussion below). Required. |
| `signalStrengthUnit` | enum | `dBm` or `dBm_estimated_from_percent`. Required -- see below. |
| `source` | enum | `networkmanager` or `connman`. Required. |

Plus best-effort optional fields (`ssid`, `frequencyMhz`, `lastSeenMs`,
`signalStrengthRawPercent`) carried through when available. Full detail in
`schema.json`.

### The signal-strength unit problem -- resolved: expose the real dBm, don't estimate it

The task brief was explicit that NM has historically used a 0-100 percentage scale
rather than dBm, and asked us to verify rather than assume a conversion. Having read
the actual source, the situation is better than "pick the least-wrong estimate
formula": **both NetworkManager and ConnMan already have the real, calibrated dBm
value in hand at the exact moment they compute their respective lossy percent, and
both discard it one line later.** Neither project needs a new *measurement* -- both
need to stop throwing an already-measured value away.

**NetworkManager**, `src/core/supplicant/nm-supplicant-interface.c:682`: reads
`v_i16`, a real dBm-scale value straight from wpa_supplicant's own `Signal` D-Bus
property (itself sourced from the driver), and immediately calls
`nm_wifi_utils_level_to_quality(v_i16)` -- only the resulting 0-100 percent
(`p_signal_percent`) is stored anywhere (`bss_info->signal_percent`,
`nm-supplicant-types.h`). `v_i16` is a local variable; nothing in `NMWifiAP`'s
private struct (`src/core/devices/wifi/nm-wifi-ap.c`) even has a field that could
hold it. `AccessPoint.Strength`'s introspection doc ("The current signal quality of
the access point, in percent.", type `y`) confirms only the percent is D-Bus-visible
today.

**ConnMan**, `plugins/wifi.c:2851`: `strength = 120 + g_supplicant_network_get_signal(...)`,
where `g_supplicant_network_get_signal()` (`gsupplicant/gsupplicant.h`, return type
`dbus_int16_t`) returns a real dBm value from the *same* underlying source
(wpa_supplicant's BSS `Signal` property) -- and, same pattern, only the computed
percent gets stored into `struct connman_network` (`src/network.c`); the real dBm
return value is discarded in the same function.

Separately, for the record: NM's source does contain three mutually-inconsistent
percent-*generating* formulas depending on backend (nl80211 `[-90,-20]`→`[30,100]`
in `nm-wifi-utils-nl80211.c`; legacy WEXT, same shape; iwd/supplicant
`[-100,-40]`→`[0,100]` in `nm-core-utils.c`'s `nm_wifi_utils_level_to_quality()`) --
this was the original finding that prompted checking for a "best" percent-to-dBm
formula. It turned out to be the wrong question: none of those three formulas need
reversing, because the real dBm each one *started from* is still sitting in a local
variable a few lines earlier in every case that matters here (the wpa_supplicant
D-Bus signal path, confirmed above; the direct-nl80211-kernel path is very likely the
same shape as it computes from a real dBm kernel value too, but wasn't traced line-
by-line the way the supplicant path was -- flagged in Open Questions below for the
patch author to confirm before assuming it generalizes).

**Design decision:** the schema's primary `signalStrength` field is real dBm,
sourced from the value both patches now retain instead of discard --
`signalStrengthUnit: "dBm"` is the normal, expected case once both patches land.
`signalStrengthRawPercent` is kept as an always-available secondary field (cheap to
also carry, useful for debugging/cross-checking), and `signalStrengthUnit:
"dBm_estimated_from_percent"` is kept in the schema only as an **interim fallback
for `reference_client.py` querying an *unpatched* NM/ConnMan today** -- not as an
expected steady-state value once the real patches ship. See `schema.json`'s updated
field descriptions.

### Why dBm (RSSI), not SNR

Settled, not left as an open question: the schema uses RSSI (received power, dBm),
never SNR (signal-to-noise ratio). Distance/path-loss estimation -- the entire point
of this schema -- depends on received power relative to a known transmit power,
which is exactly what RSSI measures. SNR instead measures signal relative to the
*receiver's local noise floor*, a property of whatever RF environment the scanning
device itself sits in (other 2.4/5GHz traffic, interference, receiver design) --
unrelated to the AP's distance. Two receivers equidistant from the same AP can have
near-identical RSSI but very different SNR depending on their local noise
environment; weighting by SNR would inject that noise-environment bias directly into
a location estimate. This also matches how every real-world WiFi geolocation system
(Google, WiGLE, Mozilla Ichnaea, Apple) actually works -- all RSSI-based, none use
SNR for this purpose.

### BSSID (`macAddress`)

NetworkManager: direct mapping, `AccessPoint.HwAddress` -- documented as "The
hardware address (BSSID) of the access point", type `s`, already in the exact
`aa:bb:cc:dd:ee:ff`-shaped string NM emits (confirmed by reading
`nm-access-point.c`/the property doc comment; no case-folding done here beyond
matching the schema's lower-case pattern, implementers should confirm NM's actual
casing convention against a live box since the introspection doc comment does not
specify case).

ConnMan: **not currently available at all** -- see below, this is the headline
finding of this task.

## What each patch needs to add

### NetworkManager -- small, additive patch

NetworkManager already has everything this contract needs, just not packaged as one
call:

- `Device.Wireless.GetAllAccessPoints()` already returns every visible AP (including
  hidden-SSID ones), each as an object implementing
  `org.freedesktop.NetworkManager.AccessPoint`.
- That interface already exposes `HwAddress` (BSSID), `Strength` (percent, see
  conversion discussion above), `Ssid`, `Frequency`, `LastSeen`, `MaxBitrate`,
  `Mode`, and security-flag properties -- i.e. everything the schema's optional
  fields want, plus more than we use.
- The NM patch has two parts, not one:
  1. **Retain the real dBm.** Add a field (e.g. `gint16 strength_dbm`) alongside
     `signal_percent` in the relevant supplicant-types struct
     (`nm-supplicant-types.h`) and in `NMWifiAP`'s private struct
     (`nm-wifi-ap.c`/`.h`), and store `v_i16` into it right next to the existing
     `nm_wifi_utils_level_to_quality(v_i16)` call at
     `nm-supplicant-interface.c:682`, instead of letting it fall out of scope. Expose
     it as a new `SignalDbm` (`type="n"`, signed 16-bit) property on
     `org.freedesktop.NetworkManager.AccessPoint`, alongside the existing `Strength`.
     Do the equivalent for the nl80211/WEXT paths too (`nm-wifi-utils-nl80211.c`,
     `nm-wifi-utils-wext.c`) once confirmed they have the same discard-after-compute
     shape (see Open Questions) -- this is the path real hardware most likely uses,
     so getting it right matters more than the supplicant D-Bus path traced above.
  2. **Reshape into the common schema.** Iterate `GetAllAccessPoints()`, read each
     AP object's properties (now including the new real `SignalDbm`), re-shape into
     `schema.json`'s record shape, and expose that array from a new
     `GetScanResults()` method on the new interface. With (1) done, every record can
     carry `signalStrengthUnit: "dBm"` -- no estimation needed.

### ConnMan -- meaningfully larger patch (headline finding)

**ConnMan's D-Bus API does not expose BSSID or per-AP signal strength anywhere,
and -- more importantly -- ConnMan does not even retain BSSID once scan data crosses
from the wpa_supplicant integration layer into ConnMan's own internal service/network
model.** This was the single most important thing this task set out to determine,
and it is a real gap, not just a missing D-Bus getter. Traced through the real
source:

1. **`net.connman.Service`, the D-Bus-visible per-network object** (aggregates by
   SSID+security, roughly analogous to "a network you could connect to", not "one
   physical AP you saw in a scan") exposes `Strength` (uint8, 0-100, "a normalized
   value" per `../connman/doc/service-api.txt`) and nothing resembling a BSSID/MAC
   field anywhere in the documented properties. Confirmed by reading the full
   property list in `doc/service-api.txt` and grepping `src/service.c`'s D-Bus
   property-dict builder (`connman_dbus_dict_append_basic(dict, "Strength", ...)`
   at `src/service.c:5305`) for any `"BSSID"` key -- there is none.
2. **`net.connman.Technology.Scan()`** triggers a scan and blocks until done, but
   results only flow out via the `Manager.ServicesChanged` signal / the same
   `Service` objects above -- there is no separate raw-scan-results method or
   signal anywhere in `doc/manager-api.txt` or `doc/technology-api.txt`.
3. **ConnMan's internal `struct connman_network`** (`src/network.c`, the per-scanned-
   network struct that later gets folded into a `connman_service`) has fields for
   `strength` (uint8), `frequency`, `identifier`, `name`, and a `wifi` sub-struct
   with `ssid`/`mode`/`security`/etc. -- **no `bssid`/`hwaddr` field exists on this
   struct at all.**
4. The percent `strength` value stored there is computed in
   `plugins/wifi.c:calculate_strength()` as `strength = min(100, 120 + signal_dbm)`
   -- yet another different, simpler linear dBm->percent formula than any of NM's
   three (see above), for what it's worth if a future common-schema author wants to
   compare backends' native lossiness.
5. Going one layer deeper, into ConnMan's wpa_supplicant D-Bus binding
   (`gsupplicant/supplicant.c`): the **raw per-BSS data absolutely does include a
   BSSID** -- `struct g_supplicant_bss` (an individual physical AP/BSS, one SSID can
   have several) has a `bssid[6]` field, populated straight from wpa_supplicant's
   own `BSSID` D-Bus property (`gsupplicant/supplicant.c:2000-2009`). So the
   information exists, transiently, at the lowest layer ConnMan talks to.
6. But `struct g_supplicant_bss` is aggregated into a `GSupplicantNetwork` (one per
   SSID, not one per physical AP) before anything above `gsupplicant/` ever sees it,
   and `gsupplicant/gsupplicant.h`'s public API only exposes
   `g_supplicant_network_get_signal()` -- there is **no**
   `g_supplicant_network_get_bssid()` or equivalent. `plugins/wifi.c`, the only
   caller that turns supplicant data into `connman_network` objects, therefore has
   no way to retrieve a BSSID even if `connman_network` had a field to put it in.

**Net effect:** the ConnMan patch is not "add a D-Bus getter for data ConnMan
already has" -- it is a three-layer plumbing job: (a) add a public
`g_supplicant_network_get_bssid()`-equivalent to `gsupplicant/gsupplicant.h` /
`supplicant.c` (noting `GSupplicantNetwork` may need to become BSS-aware rather than
purely SSID-aggregated, since today one `GSupplicantNetwork` can represent multiple
physical BSSes and the aggregation itself may need rethinking to report one record
per BSSID rather than one per SSID); (b) add a `bssid` field to
`struct connman_network` in `src/network.c` and populate it from (a) in
`plugins/wifi.c`; (c) expose it over D-Bus, either bolted onto the existing
`Service` object or via the new common `GetScanResults()`-shaped interface directly.
This is a substantially bigger change than the NetworkManager patch, and touches
ConnMan's core service/network data model, not just its D-Bus surface -- flag this
prominently to whoever scopes the two patch efforts.

Same real-dBm-already-available finding as NetworkManager applies here too, and
should be folded into the same patch rather than done separately:
`g_supplicant_network_get_signal()` (`gsupplicant/gsupplicant.h`) already returns
real dBm publicly -- `plugins/wifi.c:calculate_strength()` (line 2851,
`strength = min(100, 120 + signal_dbm)`) already has it in hand and only stores the
computed percent. Add a `signal_dbm` field to `struct connman_network`
(`src/network.c`, next to the BSSID field from (b) above) and populate it from the
same already-public getter, no new gsupplicant-layer work needed for this part
specifically (unlike BSSID, which needs the new getter in step (a)).

## Access control: both patches now implement this, not just document it

Both `../networkmanager/` and `../connman/` have working commits (see each repo's
own branch) implementing the two data-availability fixes above and the strawman
`GetScanResults()` method. A close look at each project's actual D-Bus bus policy
and PolicyKit usage found one real bug and one open design question, both now
addressed as real patch content rather than left as documentation:

- **NetworkManager**: the new `Device.WifiGeolocation1` interface was initially
  committed without a matching entry in
  `src/core/org.freedesktop.NetworkManager.conf` -- under that file's default-deny
  policy, this made `GetScanResults()` unreachable by any non-root caller, a
  functional bug independent of the privacy question. Fixed: the interface is now
  allowed, matching `AccessPoint`'s existing world-readable precedent (BSSID via
  `HwAddress` has always been readable this way -- this isn't a new category of
  exposure, just more convenient packaging of it). `RequestScan` is PolicyKit-gated
  (`org.freedesktop.NetworkManager.wifi.scan`); read-only enumeration
  (`GetAllAccessPoints`, and now `GetScanResults`) never has been -- the new method
  deliberately matches that existing precedent rather than inventing a new gate.
- **ConnMan**: no equivalent bug -- its bus policy (`src/connman-dbus.conf`) has no
  per-interface rules at all, just root / any `at_console="true"` local console user
  / nothing, so the new `BSSID`/`StrengthDbm` properties and `GetScanResults()`
  automatically inherited the same reachability as the pre-existing `Strength`
  percent with no config change needed. The real finding here: BSSID wasn't exposed
  by ConnMan *at all* before this patch, and is now reachable to that same broad
  audience. ConnMan's optional PolicyKit plugin (`plugins/polkit.c`) only defines
  `modify` and `secret` privilege buckets, neither of which covers read-only,
  non-secret data like this -- so even with PolicyKit enabled, none of this new data
  would be gated by it today.

Both patches deliberately stop short of inventing a new, more restrictive gate
(e.g. a dedicated PolicyKit action) unilaterally -- that's exactly the kind of
upstream-maintainer bikeshed already being left open for the interface names
themselves, and is called out explicitly in both patches' commit messages and code
comments as a discussion point for real patch review, not a decision made here.

## Open questions / needs live-hardware verification

- **NM `HwAddress` casing**: confirmed the property exists and is documented as the
  BSSID, but the introspection XML doc comment doesn't pin down case (upper vs.
  lower hex). Needs a live `busctl`/`gdbus` call against a real `NetworkManager` to
  confirm, since `schema.json`'s `macAddress` pattern currently requires lower-case.
- **NM's nl80211/WEXT paths' real-dBm availability**: confirmed for the
  wpa_supplicant D-Bus signal path (`nm-supplicant-interface.c:682`); the
  direct-nl80211-kernel path (`nm-wifi-utils-nl80211.c`, very likely what's actually
  active on Home Assistant Green's wifi chipset/driver) was read but not traced
  line-by-line the same way -- needs confirming it also has a real dBm value in a
  local variable immediately before its own percent formula runs, before assuming
  the "retain instead of discard" patch shape generalizes to it.
- **ConnMan `GSupplicantNetwork`'s SSID-vs-BSS aggregation in practice**: source
  reading shows the *data model* aggregates by SSID, but the real behavior of how
  many `GSupplicantNetwork`s show up per physical scan, and whether ConnMan's own
  BSS-selection/roaming logic elsewhere already has code that walks per-BSS data
  that could be reused/exposed more cheaply than this doc assumes, would benefit
  from a maintainer/list conversation and not just a source read.
- **ConnMan `Scan()` blocking behavior with a long D-Bus timeout**: documented
  behavior, not verified against a live daemon in this sandbox (no D-Bus/WiFi
  hardware access here).
- **Whether upstream NM/ConnMan maintainers would accept `org.freedesktop.*` /
  a shared interface name at all** -- flagged above as likely needing a rename
  per-project; genuinely an open question for the real patch proposals, not
  something source-reading resolves.
- Both clones here are shallow (`--depth 1`) snapshots as of the commits noted at
  the top of this doc; re-verify against a fresh clone before actually writing
  patches, since both projects are under active development.

## Files in this directory

- `README.md` -- this document.
- `schema.json` -- JSON Schema (draft 2020-12) for one access-point scan record.
- `reference_client.py` -- a `dbus-next`-based Python skeleton that detects which of
  NetworkManager/ConnMan is present on the system bus today and fetches scan results
  using each project's **real, existing, already-shipped** D-Bus API (not the
  not-yet-built common interface proposed above), reshaping the output into this
  schema on the client side as a stopgap until the patches land.

## License
 This project is dual-licensed under the **GNU General Public License v3.0** 
 and the **GNU Lesser General Public License v3.0**. See the [LICENSE](LICENSE) 
 file for the full text of both agreements.


