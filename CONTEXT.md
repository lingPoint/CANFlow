# CANFlow

CANFlow presents data recorded in BLF files as a timed visual replay.

## Language

**Playback sequence**:
A user-selected set of BLF files replayed one file at a time in a defined order.
_Avoid_: Parallel playback

**Visual replay**:
Progressive presentation of DBC-decoded signal values as waveforms in an interface, advancing as fast as each BLF file is processed. It does not imply transmission onto a CAN bus.
_Avoid_: Bus replay

**Signal waveform**:
A time series of physical signal values decoded from recorded CAN or CAN FD frames using a DBC definition.

**Capture timeline**:
The original timestamp axis shared by the playback sequence, including gaps between files.

**Playback order**:
The order of BLF files by each file's earliest recorded CAN/CAN FD frame timestamp.

**Channel DBC mapping**:
The project-configured DBC path assigned to a CAN channel for decoding that channel whenever it appears in a playback sequence. The mapping exists independently of loaded BLF recordings.

**Selected signal**:
A DBC-defined signal chosen by the user for display as a waveform.

**Unresolved selected signal**:
A project-selected signal whose DBC is missing or whose current definition no longer matches; it remains in the project until it can be reconciled or the user removes it.
_Avoid_: Deleted signal

**CANFlow project**:
A CANFlow JSON document containing channel-to-DBC path mappings and the current signal selection, but no DBC content or BLF recordings.
_Avoid_: Recording project, DBC package, BLF package

**Signal group**:
A named, globally stored reusable set of signal identities that a user explicitly applies to a project.
_Avoid_: Saved selection, chart layout

**Signal definition fingerprint**:
The stable DBC-definition summary for a signal, used alongside its identity to detect a same-named but incompatible signal when applying a signal group.
_Avoid_: Signal ID

**Chart preferences**:
User-wide waveform presentation settings shared across CANFlow projects; they do not select signals and are not stored in a project.
_Avoid_: Project chart configuration

**Signal track**:
A vertically stacked waveform lane for one selected signal, with its own value scale and the capture timeline shared by every visible track.
_Avoid_: Overlaid curve, shared vertical axis

**Focused signal track**:
The signal track currently highlighted for inspection; focusing does not hide the scales or waveforms of the other visible tracks.
_Avoid_: Primary signal

**Synchronized vertical scaling**:
A waveform interaction in which every visible signal keeps its own value range while a vertical zoom changes all visible ranges by the same scale factor.
_Avoid_: Shared vertical axis
