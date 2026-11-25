import os

import networkx as nx
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib.packet import arp, ipv4, packet
from ryu.ofproto import ofproto_v1_3
from ryu.topology import event
from ryu.topology.api import get_link, get_switch


class TopoDiscover(app_manager.RyuApp):
    OFP_VERSIONS = (ofproto_v1_3.OFP_VERSION,)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.topology_api_app = self
        self.switches = []
        self.links = []
        self.hosts = {}
        self.graph = nx.DiGraph()
        self.datapaths = {}
        self.routing_method = os.getenv('ROUTING_METHOD', 'ospf').lower()

    def _refresh_topology(self):
        """
        Consulta get_switch/get_link e atualiza as estruturas internas.
        """
        switch_list = get_switch(self.topology_api_app, None)
        link_list = get_link(self.topology_api_app, None)
        self.switches = [sw.dp.id for sw in switch_list]
        self.links = [(l.src.dpid, l.dst.dpid, l.src.port_no, l.dst.port_no)
                      for l in link_list]
        self._update_graph()

    @set_ev_cls(event.EventSwitchEnter)
    def _event_switch_enter_handler(self, ev):
        self._refresh_topology()
        self.logger.info("\nSwitches mapeados atualmente: %s", self.switches)

    @set_ev_cls(event.EventLinkAdd)
    def _event_link_add_handler(self, ev):
        self._refresh_topology()

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        arp_pkt = pkt.get_protocol(arp.arp)
        ip_pkt = pkt.get_protocol(ipv4.ipv4)

        if arp_pkt:
            # Ignora portas que levam a outros switches
            if (datapath.id, in_port) in self._switch_ports():
                return

            is_new = arp_pkt.src_ip not in self.hosts
            self.hosts[arp_pkt.src_ip] = {
                'mac': arp_pkt.src_mac,
                'dpid': datapath.id,
                'port': in_port
            }
            if is_new:
                self._update_graph()
                self.print_full_topology()

            # Se já conhecemos o host destino, entrega direto; senão, inunda só nas bordas
            dst_host = self.hosts.get(arp_pkt.dst_ip)
            if dst_host:
                dst_dp = self.datapaths.get(dst_host['dpid'])
                if dst_dp:
                    out = dst_dp.ofproto_parser.OFPPacketOut(
                        datapath=dst_dp,
                        buffer_id=dst_dp.ofproto.OFP_NO_BUFFER,
                        in_port=dst_dp.ofproto.OFPP_CONTROLLER,
                        actions=[dst_dp.ofproto_parser.OFPActionOutput(dst_host['port'])],
                        data=msg.data
                    )
                    dst_dp.send_msg(out)
            else:
                self._flood_arp_to_edges(datapath.id, in_port, msg.data)

        elif ip_pkt:
            path = self.install_path(ip_pkt.src, ip_pkt.dst, method=self.routing_method)
            if path and datapath.id in path:
                idx = path.index(datapath.id)
                if idx + 1 < len(path):
                    out_port = self.graph.edges[datapath.id, path[idx + 1]]['port']
                else:
                    out_port = self.hosts[ip_pkt.dst]['port']
                self._send_packet_out(datapath, msg, out_port)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _switch_features_handler(self, ev):
        dp = ev.msg.datapath
        self.datapaths[dp.id] = dp
        parser = dp.ofproto_parser
        ofp = dp.ofproto
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=0, match=match, instructions=inst))

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def _port_status_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp = dp.ofproto
        port_no = msg.desc.port_no

        down = (msg.desc.state & ofp.OFPPS_LINK_DOWN) or \
               (msg.desc.config & ofp.OFPPC_PORT_DOWN) or \
               msg.reason == ofp.OFPPR_DELETE
        if not down:
            return

        # Remove do grafo os enlaces e hosts que usam essa porta
        self.links = [l for l in self.links
                      if (l[0], l[2]) != (dp.id, port_no) and (l[1], l[3]) != (dp.id, port_no)]
        self.hosts = {ip: h for ip, h in self.hosts.items()
                      if (h['dpid'], h['port']) != (dp.id, port_no)}
        self._update_graph()

        # Limpa as regras IPv4 em todos os switches para forçar recálculo imediato
        self._clear_all_ip_flows()

    @set_ev_cls(event.EventLinkDelete)
    def _event_link_delete_handler(self, ev):
        self._refresh_topology()
        self._clear_all_ip_flows()

    def print_full_topology(self):
        grafo = self.graph
        switch_dpids = sorted(node for node, attrs in grafo.nodes(data=True) if attrs.get('kind') == 'switch')
        hosts_ips = sorted(node for node, attrs in grafo.nodes(data=True) if attrs.get('kind') == 'host')

        lines = ["", "================= TOPOLOGIA =================", "Switches: "]
        for dpid in switch_dpids:
            lines.append(f"  s{dpid}")

        lines.append("Enlaces entre switches:")
        for dpid_a, dpid_b, attrs in sorted(grafo.subgraph(switch_dpids).edges(data=True)):
            if dpid_a < dpid_b:
                port_b = grafo.edges[dpid_b, dpid_a]['port']
                lines.append(f"  s{dpid_a} (porta {attrs['port']}) <-> s{dpid_b} (porta {port_b})")

        lines.append("Hosts:")
        for ip in hosts_ips:
            dpid = next(iter(grafo.neighbors(ip)))
            port = grafo.edges[ip, dpid]['port']
            mac = grafo.nodes[ip]['mac']
            lines.append(f"  {ip} (MAC {mac}) -> s{dpid} (porta {port})")

        is_connected = nx.is_weakly_connected(grafo) if len(grafo) else False
        lines.append(f"Conexo: {is_connected}")
        lines.append("=============================================")
        self.logger.info("\n".join(lines))

    def _switch_ports(self):
        """
        Conjunto de (dpid, porta) usados em enlaces entre switches.
        """
        ports = set()
        for src, dst, sport, dport in self.links:
            ports.add((src, sport))
            ports.add((dst, dport))
        return ports

    def _update_graph(self):
        """
        Reconstroi o grafo networkx a partir do estado atual.
        """
        graph = nx.DiGraph()

        for dpid in self.switches:
            graph.add_node(dpid, kind='switch')
        for src, dst, sport, dport in self.links:
            graph.add_edge(src, dst, port=sport)
            graph.add_edge(dst, src, port=dport)
        for ip, h in self.hosts.items():
            graph.add_node(ip, kind='host', mac=h['mac'])
            graph.add_edge(ip, h['dpid'], port=h['port'])
            graph.add_edge(h['dpid'], ip, port=h['port'])

        self.graph = graph

    def get_route_ospf(self, switch_in, switch_out):
        try:
            return nx.shortest_path(self.graph, switch_in, switch_out)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None

    def get_route_ecmp(self, switch_in, switch_out, flow_key=""):
        try:
            paths = list(nx.all_shortest_paths(self.graph, switch_in, switch_out))
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None
        key = flow_key if flow_key else (switch_in + switch_out)
        hash_val = hash(key)
        choice = hash_val % len(paths)
        return sorted(paths)[choice]

    def find_route(self, switch_in, switch_out, method='ospf', flow_key=""):
        if method == 'ecmp':
            return self.get_route_ecmp(switch_in, switch_out, flow_key)
        return self.get_route_ospf(switch_in, switch_out)

    def add_flow(self, dp, priority, match, actions, idle_timeout=0):
        parser = dp.ofproto_parser
        ofp = dp.ofproto
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=priority, match=match,
                                      instructions=inst, idle_timeout=idle_timeout))

    def install_path(self, src_ip, dst_ip, method='ospf'):
        src, dst = self.hosts.get(src_ip), self.hosts.get(dst_ip)
        if not src or not dst:
            return None

        path = self.find_route(src['dpid'], dst['dpid'], method, src_ip + dst_ip)
        if not path:
            return None

        for hops, s_ip, d_ip, last_port in ((path, src_ip, dst_ip, dst['port']),
                                            (path[::-1], dst_ip, src_ip, src['port'])):
            for i, dpid in enumerate(hops):
                dp = self.datapaths[dpid]
                parser = dp.ofproto_parser
                if i + 1 < len(hops):
                    out_port = self.graph.edges[dpid, hops[i + 1]]['port']
                else:
                    out_port = last_port
                match = parser.OFPMatch(eth_type=0x0800, ipv4_src=s_ip, ipv4_dst=d_ip)
                actions = [parser.OFPActionOutput(out_port)]
                self.add_flow(dp, 10, match, actions, idle_timeout=30)
        return path

    def _send_packet_out(self, dp, msg, out_port):
        parser = dp.ofproto_parser
        ofp = dp.ofproto
        data = msg.data if msg.buffer_id == ofp.OFP_NO_BUFFER else None
        actions = [parser.OFPActionOutput(out_port)]
        out = parser.OFPPacketOut(
            datapath=dp,
            buffer_id=msg.buffer_id,
            in_port=msg.match['in_port'],
            actions=actions,
            data=data
        )
        dp.send_msg(out)

    def _flood_arp_to_edges(self, src_dpid, in_port, data):
        """Envia o ARP apenas pelas portas de borda (hosts), evitando loops entre switches."""
        inter_switch_ports = self._switch_ports()
        for dpid, dp in self.datapaths.items():
            parser = dp.ofproto_parser
            ofp = dp.ofproto
            actions = []
            for port_no in dp.ports:
                if port_no >= ofp.OFPP_MAX:
                    continue
                if dpid == src_dpid and port_no == in_port:
                    continue
                if (dpid, port_no) not in inter_switch_ports:
                    actions.append(parser.OFPActionOutput(port_no))
            if actions:
                out = parser.OFPPacketOut(
                    datapath=dp,
                    buffer_id=ofp.OFP_NO_BUFFER,
                    in_port=ofp.OFPP_CONTROLLER,
                    actions=actions,
                    data=data
                )
                dp.send_msg(out)

    def _clear_all_ip_flows(self):
        """Remove todas as regras de roteamento IPv4 de todos os switches, mantendo a regra table-miss."""
        for dp in self.datapaths.values():
            parser = dp.ofproto_parser
            ofp = dp.ofproto
            match = parser.OFPMatch(eth_type=0x0800)
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp,
                table_id=ofp.OFPTT_ALL,
                command=ofp.OFPFC_DELETE,
                out_port=ofp.OFPP_ANY,
                out_group=ofp.OFPG_ANY,
                match=match
            ))