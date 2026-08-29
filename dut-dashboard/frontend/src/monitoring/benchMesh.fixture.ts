import type { MeshMember, MeshProbe } from "../api/rest";

/**
 * A real mesh, captured off the bench, kept so it cannot quietly stop being one.
 *
 * Test-only: nothing in the app imports this. It exists because every mesh
 * assertion in this codebase used to be written by hand from a spec, and a
 * hand-written fixture agrees with whatever the author believed — which is the
 * one thing a test of a device's answer must not do.
 *
 * Captured 2026-08-28 over the mesh node's own console
 * (`AP6420-PA10054DDHWVF2D` on the Pi at 192.168.30.124, `/dev/ttyUSB0`),
 * through the shipped `MESH_CONSOLE_COMMAND`. The mesh was two devices:
 *
 *   root  AP6420E-PB1005QPCFVFMA8  192.168.30.121  cabled to this desk
 *   node  AP6420-PA10054DDHWVF2D   192.168.30.176  console on the Pi
 *
 * Three things in here are worth more than the round trip they cost:
 *
 *  1. **The root's `signal` is 0 and its `rssi` is null.** Zero is the field
 *     being inapplicable — a root has no parent to hear — and rendered as a
 *     number it reads as the strongest link on the bench. The backend nulls it;
 *     this fixture is what proves the nulling is still happening on real bytes.
 *
 *  2. **`node` and `node_number` DISAGREE.** The second member is `node: "0"`
 *     with `node_number: 1`. `mesh_topology.py` publishes both and its comment
 *     says they agreed on this bench — as of this capture they do not, on the
 *     device's own output. Deriving either from the other would have hidden it.
 *
 *  3. **The empty reply below is a real answer, not a failure.** Same command,
 *     same transport, same parser, on `AP6840E-PD1005VMG3KJH9C` minutes later.
 *     `error_code: 0` with an empty list is a device saying it stands alone; an
 *     error code would have meant "could not tell", which is a third thing.
 */

/** What the node's own API returned, byte for byte. */
export const BENCH_MESH_REPLY =
  '{"data":{"mesh_info_list":[' +
  '{"mac_address":"C8:4F:86:91:47:E1","node":"1","hop":1,"mesh_type":"Root",' +
  '"ip_address":"192.168.30.121","signal":0,"node_number":1},' +
  '{"mac_address":"C8:4F:86:89:F1:68","node":"0","hop":0,"mesh_type":"Node",' +
  '"ip_address":"192.168.30.176","signal":-31,"node_number":1}' +
  '],"total_size":2},"error_code":0,"error_msg":""}';

/** The same reply after the backend's parser, which is what a browser receives. */
export const BENCH_MESH_MEMBERS: MeshMember[] = [
  {
    mac: "C8:4F:86:91:47:E1",
    node: "1",
    node_number: 1,
    hop: 1,
    role: "root",
    mesh_type: "Root",
    ip: "192.168.30.121",
    // `signal: 0` above. Null here, and that difference is the point.
    rssi: null,
    rssi_band: null,
  },
  {
    mac: "C8:4F:86:89:F1:68",
    node: "0",
    node_number: 1,
    hop: 0,
    role: "node",
    mesh_type: "Node",
    ip: "192.168.30.176",
    rssi: -31,
    rssi_band: "near",
  },
];

/** The management addresses that tie those members to registry entries. */
export const BENCH_ROOT_MGMT = "https://192.168.30.121";
export const BENCH_NODE_MGMT = "https://192.168.30.176";

/** The probe as the registry stored it, for either DUT: both saw both members. */
export const BENCH_MESH_PROBE: MeshProbe = {
  probed: true,
  mesh: true,
  members: BENCH_MESH_MEMBERS,
  detail: "",
  captured_at: "2026-08-28 13:52:00",
};

/** `AP6840E-PD1005VMG3KJH9C`, asked the same way: a device with no mesh. */
export const BENCH_NO_MESH_REPLY =
  '{"data":{"mesh_info_list":[],"total_size":0},"error_code":0,"error_msg":""}';

export const BENCH_NO_MESH_PROBE: MeshProbe = {
  probed: true,
  mesh: false,
  members: [],
  detail: "The device answered with an empty mesh list.",
  captured_at: "2026-08-28 13:18:00",
};
