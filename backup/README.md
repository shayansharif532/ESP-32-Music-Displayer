# backup/

Put **your own factory flash backup** here before you flash a board for the first time: `backup/factory/<your-file>.bin`
(a full 16 MiB `esptool read-flash` dump of your unit). `*.bin` is git-ignored, so the image can never be committed by accident:
it is device-specific, 16 MiB, and can contain data from earlier users.

How to take, verify and restore a backup: [docs/development-history/M0-factory-backup.md](../docs/development-history/M0-factory-backup.md).
