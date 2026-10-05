"""Application-owned SDK adapter; canonicalization/crypto stay in OSS SDK.

OSS SDK 1.3.1 accepts Config.additional_headers but drops them while building
SigningContext. Inject via Client(config, signer=...) instead of patching SDK.
"""
from alibabacloud_oss_v2.signer import SignerV4
from alibabacloud_oss_v2.types import SigningContext


class ContentLengthSignerV4(SignerV4):
    """Preserve SDK credentials, expiry and signing, including request length."""

    def sign(self, signing_ctx: SigningContext) -> None:
        signing_ctx.additional_headers = set(signing_ctx.additional_headers or ()) | {'content-length'}
        super().sign(signing_ctx)

    @staticmethod
    def is_signed_header(header: str) -> bool:
        # SDK presigner uses this hook to build the actual upload header map.
        return header.lower() == 'content-length' or SignerV4.is_signed_header(header)
