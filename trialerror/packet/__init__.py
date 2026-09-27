"""The weekly decision packet (``trialerror packet``): everything waiting for the
operator's decision, in plain words, in one packet capped at half an hour of
reading; announced by one push notification; answers recorded for the next
session."""

from trialerror.packet.store import PacketError, PacketSettings, packet_settings  # noqa: F401
