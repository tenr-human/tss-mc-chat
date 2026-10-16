import eventlet

eventlet.monkey_patch()

import json
import eventlet.wsgi
import os
import socket
import sys
import threading

if not hasattr(eventlet.wsgi, "ALREADY_HANDLED"):
    eventlet.wsgi.ALREADY_HANDLED = object()

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from ryu.base import app_manager
from ryu.controller import event, ofp_event
from ryu.controller.handler import (
    CONFIG_DISPATCHER,
    DEAD_DISPATCHER,
    MAIN_DISPATCHER,
    set_ev_cls,
)
from ryu.ofproto import ofproto_v1_3

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
        super().__init__()
        self.data = data


class ChatQoSController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.datapaths = {}
        self.users = {}
        self.blocked_users = set()
        self.mac_to_port = {}
        listener = threading.Thread(target=self.listen_for_events, daemon=True)
        listener.start()

    def listen_for_events(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((CONTROLLER_EVENT_BIND, CONTROLLER_EVENT_PORT))
        server.listen()
        server.settimeout(1)
        self.logger.info("Listening for chat events on port %s", CONTROLLER_EVENT_PORT)
        while True:
            try:
                conn, address = server.accept()
            except socket.timeout:
                continue
            except OSError as error:
                self.logger.error("Chat event listener stopped: %s", error)
                return
            with conn:
                buffer = b""
                try:
                    while True:
                        data = conn.recv(4096)
                        if not data:
                            break
                        buffer += data
                        while b"\n" in buffer:
                            raw_event, buffer = buffer.split(b"\n", 1)
                            self.send_event(
                                self.name,
                                ChatEvent(json.loads(raw_event.decode())),
                            )
                except (OSError, ValueError) as error:
                    self.logger.error("Bad chat event from %s: %s", address, error)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_ready(self, ev):
        datapath = ev.msg.datapath
        self.datapaths[datapath.id] = datapath
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        instructions = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
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
            if user["event"] == "join":
                self.install_user_flow(datapath, user, username in self.blocked_users)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def switch_disconnected(self, ev):
        if ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(ev.datapath.id, None)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def learn_switch(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        from ryu.lib.packet import ethernet, packet as packet_lib
        parsed = packet_lib.Packet(msg.data)
        ethernet_header = parsed.get_protocol(ethernet.ethernet)
        if ethernet_header is None:
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


if __name__ == "__main__":
    from ryu.cmd.manager import main
    main()
