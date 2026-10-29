from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, set_ev_cls, CONFIG_DISPATCHER
from ryu.ofproto import ofproto_v1_3
from ryu.topology import event
from ryu.topology.api import get_switch, get_link
from ryu.lib.packet import packet, arp

import networkx as nx

class TopoDiscover(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(TopoDiscover, self).__init__(*args, **kwargs)
        self.topology_api_app = self
        self.switches = []
        self.links = []
        self.hosts = {} # Estrutura sugerida: {ip: {'mac': mac, 'dpid': dpid, 'port': port}}
        self.graph = nx.Graph()

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
        unique = {frozenset((s, d)) for s, d, _, _ in self.links}

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        in_port = msg.match['in_port']
        
        pkt = packet.Packet(msg.data)
        arp_pkt = pkt.get_protocol(arp.arp)

        if arp_pkt:
            self.logger.info("\nARP recebido do IP %s, MAC %s no Switch %s, porta %s", 
                             arp_pkt.src_ip, arp_pkt.src_mac, datapath.id, in_port)
            
            # Ignora portas que levam a outros switches
            if (datapath.id, in_port) in self._switch_ports():
                return
            
            # Salvar as informações do host descoberto na estrutura self.hosts
            self.hosts[arp_pkt.src_ip] = {
                                            'mac':  arp_pkt.src_mac,
                                            'dpid': datapath.id,
                                            'port': in_port      
                                         }
            
            # Chamar função para imprimir a topologia completa no log
            self._update_graph()
            self.print_full_topology()
            
    def print_full_topology(self):
        # Lógica iterativa para imprimir no terminal a topologia completa mapeada de forma amigável
        # (Switches disponíveis, Enlaces registrados e Servidores/Hosts conectados)
        grafo = self.graph
        switch_dpids = sorted (node for node, attrs in grafo.nodes(data=True) if attrs.get('kind') == 'switch')
        hosts_ips = sorted (node for node, attrs in grafo.nodes(data=True) if attrs.get('kind') == 'host')

        string = ["", "================= TOPOLOGIA =================", "Switches: "]
        for dpid in switch_dpids:
            string.append("  s%s" % dpid)

        string.append("Enlaces entre switches:")
        switch_links = grafo.subgraph(switch_dpids).edges(data=True)
        for dpid_a, dpid_b, attrs in sorted(
            switch_links, key=lambda link: (link[0], link[1])):
            string.append("  s%s (porta %s) <-> s%s (porta %s)"
                          % (dpid_a, attrs['ports'][dpid_a],
                             dpid_b, attrs['ports'][dpid_b]))

        string.append("Hosts:")
        for ip in hosts_ips:
            dpid = next(iter(grafo.neighbors(ip)))
            port = grafo.edges[ip, dpid]['ports'][dpid]
            string.append("  %s (MAC %s) -> s%s (porta %s)"
                          % (ip, grafo.nodes[ip]['mac'], dpid, port))

        # Verificacao extra: True se ha caminho entre quaisquer dois nos. False indica descoberta incompleta
        string.append("Conexo: %s" % (nx.is_connected(grafo) if len(grafo) else False))
        string.append("=============================================")
        self.logger.info("\n".join(string))
            
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _switch_features_handler(self, ev):
        dp = ev.msg.datapath
        parser = dp.ofproto_parser
        ofp = dp.ofproto
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=0, match=match, instructions=inst))

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
        graph = nx.Graph()

        for dpid in self.switches:
            graph.add_node(dpid, kind='switch')
        for src, dst, sport, dport in self.links:
            graph.add_edge(src, dst, ports={src: sport, dst: dport})
        for ip, h in self.hosts.items():
            graph.add_node(ip, kind='host', mac=h['mac'])
            graph.add_edge(ip, h['dpid'], ports={h['dpid']: h['port']})

        self.graph = graph