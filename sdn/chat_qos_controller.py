import json
import os
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from os_ken.base import app_manager
from os_ken.controller import event, ofp_event
from os_ken.controller.handler import (
    CONFIG_DISPATCHER,
    DEAD_DISPATCHER,
    MAIN_DISPATCHER,
    set_ev_cls,
)
from os_ken.lib import hub
from os_ken.lib.packet import ethernet, ether_types, packet
from os_ken.ofproto import ofproto_v1_3

from config import (
    CHAT_QUEUE_ID,
    CONTROLLER_EVENT_BIND,
    CONTROLLER_EVENT_PORT,
    PORT,
    SERVER_IP,
    SERVER_SWITCH_PORT,
    SWITCH_LOCAL_MAC,
)


class ChatEvent(event.EventBase):
    def __init__(self, data):
        super(ChatEvent, self).__init__()
        self.data = data


class ChatQoSController(app_manager.OSKenApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.datapaths = {}
        self.users = {}
        self.blocked_users = set()
        self.mac_to_port = {}
        self.event_server = hub.StreamServer(
            (CONTROLLER_EVENT_BIND, CONTROLLER_EVENT_PORT),
            self.handle_chat_connection,
        )
        hub.spawn(self.event_server.serve_forever)
        self.logger.info(
            "Listening for chat events on %s:%s",
            CONTROLLER_EVENT_BIND,
            CONTROLLER_EVENT_PORT,
        )

    def handle_chat_connection(self, conn, address):
        buffer = b""
        try:
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                buffer += data
                while b"\n" in buffer:
                    raw_event, buffer = buffer.split(b"\n", 1)
                    if not raw_event.strip():
                        continue
                    try:
                        message = json.loads(raw_event.decode("utf-8"))
                        if not isinstance(message, dict):
                            raise ValueError("event must be a JSON object")
                        self.send_event(self.name, ChatEvent(message))
                    except (UnicodeDecodeError, ValueError) as error:
                        self.logger.warning(
                            "Invalid chat event from %s: %s", address, error
                        )
        except OSError as error:
            self.logger.warning(
                "Chat event connection from %s failed: %s", address, error
            )
        finally:
            conn.close()

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_ready(self, ev):
        datapath = ev.msg.datapath
        self.datapaths[datapath.id] = datapath
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        match = parser.OFPMatch()
        actions = [
            parser.OFPActionOutput(
                ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER
            )
        ]
        instructions = [
            parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)
        ]
        flow = parser.OFPFlowMod(
            datapath=datapath,
            priority=0,
            match=match,
            instructions=instructions,
        )
        datapath.send_msg(flow)
        local_match = parser.OFPMatch(eth_dst=SWITCH_LOCAL_MAC)
        local_actions = [parser.OFPActionOutput(ofproto.OFPP_LOCAL)]
        local_instructions = [
            parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, local_actions)
        ]
        datapath.send_msg(
            parser.OFPFlowMod(
                datapath=datapath,
                priority=10,
                match=local_match,
                instructions=local_instructions,
            )
        )
        for username, user in self.users.items():
            if user.get("event") == "join":
                self.install_user_flow(
                    datapath, user, username in self.blocked_users
                )

    @set_ev_cls(
        ofp_event.EventOFPStateChange,
        [MAIN_DISPATCHER, DEAD_DISPATCHER],
    )
    def switch_disconnected(self, ev):
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[ev.datapath.id] = ev.datapath
        elif ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(ev.datapath.id, None)
            self.mac_to_port.pop(ev.datapath.id, None)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def learn_switch(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        parsed = packet.Packet(msg.data)
        ethernet_header = parsed.get_protocol(ethernet.ethernet)
        if (
            ethernet_header is None
            or ethernet_header.ethertype == ether_types.ETH_TYPE_LLDP
        ):
            return

        source = ethernet_header.src
        destination = ethernet_header.dst
        in_port = msg.match["in_port"]
        self.mac_to_port.setdefault(datapath.id, {})[source] = in_port
        out_port = self.mac_to_port[datapath.id].get(destination, ofproto.OFPP_FLOOD)
        actions = [parser.OFPActionOutput(out_port)]

        if out_port != ofproto.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_dst=destination)
            instructions = [
                parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)
            ]
            datapath.send_msg(
                parser.OFPFlowMod(
                    datapath=datapath,
                    priority=1,
                    match=match,
                    instructions=instructions,
                )
            )
        data = None if msg.buffer_id != ofproto.OFP_NO_BUFFER else msg.data
        datapath.send_msg(
            parser.OFPPacketOut(
                datapath=datapath,
                buffer_id=msg.buffer_id,
                in_port=in_port,
                actions=actions,
                data=data,
            )
        )

    @set_ev_cls(ChatEvent)
    def chat_event(self, ev):
        data = ev.data
        username = data.get("user", "")
        action = data.get("event", "")
        if action in ("register", "join"):
            self.users[username] = data
            if action == "join":
                for datapath in self.datapaths.values():
                    self.install_user_flow(
                        datapath, data, username in self.blocked_users
                    )
        elif action == "leave":
            user = self.users.pop(username, data)
            for datapath in self.datapaths.values():
                self.delete_user_flow(datapath, user)
        elif action in ("block", "unblock"):
            if action == "block":
                self.blocked_users.add(username)
            else:
                self.blocked_users.discard(username)
            user = self.users.get(username)
            if user and user.get("event") == "join":
                for datapath in self.datapaths.values():
                    self.install_user_flow(
                        datapath, user, username in self.blocked_users
                    )
        self.logger.info(
            "Chat event: %s user=%s room=%s",
            action,
            username,
            data.get("room", ""),
        )

    def user_match(self, datapath, user):
        parser = datapath.ofproto_parser
        return parser.OFPMatch(
            eth_type=0x0800,
            ip_proto=6,
            ipv4_src=user["ip"],
            tcp_src=int(user["port"]),
            ipv4_dst=SERVER_IP,
            tcp_dst=PORT,
        )

    def install_user_flow(self, datapath, user, blocked):
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        self.delete_user_flow(datapath, user)
        match = self.user_match(datapath, user)
        if blocked:
            instructions = []
            priority = 200
        else:
            actions = [
                parser.OFPActionSetQueue(CHAT_QUEUE_ID),
                parser.OFPActionOutput(SERVER_SWITCH_PORT),
            ]
            instructions = [
                parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)
            ]
            priority = 100
        datapath.send_msg(
            parser.OFPFlowMod(
                datapath=datapath,
                priority=priority,
                match=match,
                instructions=instructions,
            )
        )
        self.logger.info(
            "%s chat flow for %s (%s:%s)",
            "Blocked" if blocked else "Prioritised",
            user.get("user", ""),
            user["ip"],
            user["port"],
        )

    def delete_user_flow(self, datapath, user):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        datapath.send_msg(
            parser.OFPFlowMod(
                datapath=datapath,
                command=ofproto.OFPFC_DELETE,
                out_port=ofproto.OFPP_ANY,
                out_group=ofproto.OFPG_ANY,
                match=self.user_match(datapath, user),
            )
        )
