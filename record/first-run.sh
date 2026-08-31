#!/bin/bash
# Servis ayaga kalkinca ilk canli turu bir kez calistirir.
set -e
cd /opt/prereg
export PYTHONPATH=/opt/prereg PREREG_HOME=/opt/prereg/record
export PREREG_PASSPHRASE=$(cat /root/.config/prereg/passphrase)
export PREREG_IDENTITY_PEM=$(base64 -w0 /root/.config/prereg/identity.pem)
python3 -m prereg.cli publish-did || true
python3 -m prereg.cli run --room mb-prereg --domains network --cycles 1 --interval 0
python3 -m prereg.cli status --room mb-prereg
