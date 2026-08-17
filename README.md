# Open Research Tools APT repository

This repository publishes the signed APT package index for Open Research Tools.

## Archive signing key

The public archive key is available in binary and ASCII-armored forms:

- `keys/openresearchtools-archive-keyring.gpg`
- `keys/openresearchtools-archive-keyring.asc`

Fingerprint:

```text
94A2 D5BD BD2A 22C1 B6F0 914D C7B3 EFA0 BA41 EB0A
```

Only the public archive key belongs in Git. The private primary key and CI
signing subkey must never be committed, even in encrypted form. The CI signing
subkey is held in the protected `apt-signing` GitHub environment. The encrypted
primary-key recovery copy is stored separately from this repository.
