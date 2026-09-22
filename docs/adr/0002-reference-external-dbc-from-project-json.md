# Reference external DBC files from project JSON

CANFlow V2 stores channel-to-DBC paths, DBC hashes, and selected signal fingerprints in a versioned `.canflow.json` document, while deliberately excluding DBC contents and BLF recordings. This keeps projects small and lets users manage DBC files directly, at the cost of portability; relative paths, absolute-path fallback, change detection, and unresolved signal preservation make that trade-off explicit and recoverable.
