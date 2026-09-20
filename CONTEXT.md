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
The order of BLF files by the timestamp of each file's first frame.

**Channel DBC mapping**:
The DBC definition assigned to one recorded CAN channel for decoding its frames throughout a playback sequence.

**Selected signal**:
A DBC-defined signal chosen by the user for display as a waveform.
