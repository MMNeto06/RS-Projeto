class Router:

    async def _route_attack_request(self, req, target_peer: dict = None):
        # ── Modo lobby ────────────────────────────────────────────
        if self.lobby and self.lobby.session:
            if self.lobby.is_host:
                # Somos o host: processamos localmente via HostAttack
                local_addr = f"{self.host}:{self.grpc_port}"
                return await self.send_rpc(local_addr, 'HostAttack', req)
            else:
                # Não somos o host: reencaminhamos para o host
                host_addr = self.lobby.host_addr
                if host_addr:
                    return await self.send_rpc(host_addr, 'HostAttack', req)
                self.state.log_event("[!] Host desconhecido — ataque bloqueado.")
                return None

        # ── Sem lobby: P2P direto para o alvo ─────────────────────
        if target_peer and target_peer.get("grpc_addr"):
            result = await self.send_rpc(target_peer["grpc_addr"], 'Attack', req)
            if result is None:
                self.state.log_event("[!] Ataque falhou.")
            return result

        self.state.log_event("[!] Não foi possível enviar o ataque.")
        return None


    async def _route_move_request(self, req):
        # ── Modo lobby ────────────────────────────────────────────
        if self.lobby and self.lobby.session:
            if self.lobby.is_host:
                local_addr = f"{self.host}:{self.grpc_port}"
                return await self.send_rpc(local_addr, 'HostMove', req)
            else:
                host_addr = self.lobby.host_addr
                if host_addr:
                    return await self.send_rpc(host_addr, 'HostMove', req, timeout=1.0)
                return None

        # ── Sem lobby: aplica localmente e difunde ─────────────────
        self.state.local.x = req.x
        self.state.local.y = req.y
        await self.broadcast_state()
        return None


    async def route_attack(self, req, target_peer: dict = None):
        return await self._route_attack_request(req, target_peer)

    async def route_move(self, req):
        return await self._route_move_request(req)