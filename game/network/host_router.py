class Router:
    
    async def _route_attack_request(self, req, target_peer: dict = None):
        result = None
        if self.lobby and not self.lobby.is_host:
            host_addr = self.lobby.host_addr

            if host_addr:
                result = await self.send_rpc(host_addr, 'HostAttack', req)
            else:
                self.state.log_event("[!] Não foi possível enviar o ataque.")
                return None
        else:
            local_host_addr = f"{self.host}:{self.grpc_port}"
            result = await self.send_rpc(local_host_addr, 'HostAttack', req)

        if result is None:
            self.state.log_event("[!] Ataque falhou.")

        return result
    
    
    async def _route_move_request(self, req):
        result = None
        if self.lobby and not self.lobby.is_host:
            host_addr = self.lobby.host_addr

            if host_addr:
                result = await self.send_rpc(host_addr, 'HostMove', req)
            else:
                return None
        else:
            local_host_addr = f"{self.host}:{self.grpc_port}"
            result = await self.send_rpc(local_host_addr, 'HostMove', req)

        if result is None:
            self.state.log_event("[!] Move failed.")

        return result


    async def route_attack(self, req, target_peer: dict = None):
        #adapter
        return await self._route_attack_request(req, target_peer)
    
    async def route_move(self, req):
        return await self._route_move_request(req)



