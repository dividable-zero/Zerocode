import logging
import discord
import davey
from discord.ext.voice_recv import opus as _vr_opus

log = logging.getLogger("zerocode.dave_patch")


def apply_voice_recv_patch() -> None:
    """Decrypt DAVE E2EE voice frames when active and prevent OpusError crashes."""
    if getattr(_vr_opus.PacketDecoder, "_zc_patched", False):
        return

    _orig_decode_packet = _vr_opus.PacketDecoder._decode_packet

    def _safe_decode_packet(self, packet):
        if packet is not None and getattr(packet, "decrypted_data", None) and len(packet.decrypted_data) > 3:
            sink = getattr(self, "sink", None)
            vc = getattr(sink, "voice_client", None)
            conn = getattr(vc, "_connection", None) if vc is not None else None
            dave_session = getattr(conn, "dave_session", None) if conn is not None else None
            dave_version = getattr(conn, "dave_protocol_version", 0) if conn is not None else 0

            if dave_session is not None and dave_version > 0:
                user = getattr(self, "user", None)
                user_id = getattr(user, "id", None)
                if user_id is not None:
                    try:
                        decrypted = dave_session.decrypt(
                            int(user_id),
                            davey.MediaType.audio,
                            bytes(packet.decrypted_data),
                        )
                        if decrypted:
                            packet.decrypted_data = decrypted
                    except Exception as exc:
                        log.warning("DAVE decrypt failed for user %s: %s", user_id, exc)

        try:
            return _orig_decode_packet(self, packet)
        except discord.opus.OpusError:
            return packet, b"\x00" * 3840

    _vr_opus.PacketDecoder._decode_packet = _safe_decode_packet
    _vr_opus.PacketDecoder._zc_patched = True
    log.info("Applied DAVE E2EE voice receive patch.")
