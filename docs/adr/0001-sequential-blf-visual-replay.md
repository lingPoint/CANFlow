# Replay BLF files sequentially on their original timeline

CANFlow processes selected BLF files in first-frame timestamp order, finishing one file before starting the next. This preserves the recorder's file boundaries and original timestamp gaps while avoiding ambiguous interleaving of overlapping recordings. A next file whose first timestamp is earlier than the previous file's last timestamp blocks replay with an error; equal boundary timestamps are allowed. The plotted data advances as fast as processing allows, without waiting for elapsed capture time.
