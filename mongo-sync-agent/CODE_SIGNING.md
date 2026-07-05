# Code signing (Authenticode) — future setup

> **Status: not yet implemented.** The released `msa.exe` is currently
> **unsigned**. This document captures the process and options so signing can
> be added later without re-researching it. It is not required for the internal
> beta, where the executable can simply be allow-listed in the testing area.

## Why sign

The one-file PyInstaller `msa.exe` is a worst-case trigger for enterprise
antivirus and Windows SmartScreen: unsigned + single-file + a bundled Python
runtime. Authenticode signing:

- replaces the "Unknown publisher" prompt with a verified publisher name,
- lets SmartScreen and most enterprise AV trust the download instead of
  quarantining it,
- makes any post-signing tampering detectable.

For an internal beta this is optional. Before **external client** distribution
it is strongly recommended.

## The key constraint (read this first)

Since June 2023 the CA/Browser Forum requires **all** code-signing private keys
to be held on certified hardware — an HSM, a USB token, or a cloud HSM. You can
no longer download a plain `.pfx` and drop it into CI. This makes *how* you sign
(where the key lives) more important than the certificate price, and it is the
main thing that shapes the CI integration.

## Options

| Option | ~Cost | CI integration | Notes |
|---|---|---|---|
| **Azure Trusted Signing** | ~$10/month (~$120/yr) | Best — official GitHub Action, cloud HSM, no token | **Requires** the signing org to prove a legal identity ≥ 3 years old (or extra vetting). Best value for this tool. |
| Traditional **OV** cert + USB token | ~$100–400/yr + token | Poor — a physical token does not fit hosted runners; needs a self-hosted/attended signer | SmartScreen reputation accrues slowly over downloads. |
| **EV** cert via cloud signing (DigiCert KeyLocker, SSL.com eSigner, …) | ~$250–700/yr | Via the vendor's cloud signing API | **Instant** SmartScreen reputation (no warm-up). Strictest org vetting. |

**Recommendation:** Azure Trusted Signing, assuming The Information Lab's
registered entity meets the ≥ 3-year identity check. If day-one zero-friction
SmartScreen is required, use an EV cert via a cloud-signing vendor.

## One-time setup (Azure Trusted Signing path)

1. Create a **Trusted Signing account** and a **certificate profile** in Azure.
2. Complete **organization/identity validation** (business documents + a
   callback). Allow days-to-weeks — start this early; it is the long pole.
3. Create a service principal / federated credential for GitHub Actions and add
   its details as repository **secrets** (tenant/client id, account name,
   profile name).

## CI integration (release.yml)

Add a signing step **after** the PyInstaller build and **before** the bundle is
assembled, so the signed exe is what ships:

```yaml
      - name: Sign msa.exe
        uses: azure/trusted-signing-action@v0        # pin to a specific version
        with:
          azure-tenant-id: ${{ secrets.AZURE_TENANT_ID }}
          azure-client-id: ${{ secrets.AZURE_CLIENT_ID }}
          azure-client-secret: ${{ secrets.AZURE_CLIENT_SECRET }}
          endpoint: https://eus.codesigning.azure.net/     # your region
          trusted-signing-account-name: ${{ secrets.SIGNING_ACCOUNT }}
          certificate-profile-name: ${{ secrets.SIGNING_PROFILE }}
          files-folder: mongo-sync-agent/dist
          files-folder-filter: exe
```

The equivalent low-level call is `signtool sign /fd SHA256 /tr
<timestamp-url> /td SHA256 dist\msa.exe`. **Always timestamp** (`/tr … /td
SHA256`) — a timestamped signature stays valid after the signing certificate
expires; an un-timestamped one does not.

## Verifying a signature

On any Windows host:

```powershell
Get-AuthenticodeSignature .\msa.exe | Format-List Status, SignerCertificate, TimeStamperCertificate
# Status should be 'Valid'
```

## When to revisit

Trigger this work when the first **external client** rollout is scheduled.
Budget for: certificate/account cost, the organization-validation lead time,
and wiring + testing the CI signing step. Until then, keep the RELEASING.md
note that the exe is unsigned.
