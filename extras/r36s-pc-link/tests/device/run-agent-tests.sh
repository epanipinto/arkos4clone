#!/bin/bash
# The agent tests on fake hardware, inside the dArkOS4Clone chroot (see chroot-run.sh).
. /w/tests/device/fakehw.sh
python3 /w/tests/device/test_agent.py -v
