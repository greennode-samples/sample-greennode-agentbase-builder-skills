# Private networking — reaching internal systems

Source: [Private Networking](https://docs.greennode.ai/ai-stack/agent-base/private-networking). Reference implementation for an on-prem MCP server over VPN: [greennode-samples/sample-onprem-mcp-vpn](https://github.com/greennode-samples/sample-onprem-mcp-vpn).

## Public vs Private

Agent Runtime and MCP Gateway each have a network mode, chosen **at creation**:

| | Public (default) | Private |
|---|---|---|
| Access | AgentBase shared public gateway | A private gateway dedicated to your org |
| Traffic | Over the internet | Stays on the private network (VPC Peering) |
| Reaches your VPC / on-prem | No | Yes |
| Prerequisite | — | **VPC Peering** between your VPC and AgentBase — request it from GreenNode support; only peered VPCs appear in the dropdown |

## Which component goes Private

| Situation | Private component | Why |
|---|---|---|
| Tools call an internal DB/API directly from agent code | **Runtime** | The container needs a route into the VPC |
| Internal system exposed as an MCP server (recommended, see decision guide §4) | **MCP Gateway** | The Gateway keeps inbound auth + Policy Group + audit; the agent Runtime can stay Public |
| MCP server or data in your **data center** | **MCP Gateway** + VPN Site-to-Site from the VPC to the DC | Data never leaves the DC; see the sample repo |
| Self-hosted Langfuse / other services in the VPC | **Runtime** | Exporter traffic goes to a private address |

Prefer the **MCP Gateway** path: one governed entry point, per-tool policy, credentials in Access Control (the agent never sees them).

## Fields

| | Runtime (*Network settings* step) | MCP Gateway (*Network & Compute* step) |
|---|---|---|
| VPC | required — peered VPC | required — peered VPC |
| Subnet | required | required (shows Zone and CIDR) |
| Route CIDRs | optional — other subnets / the on-prem CIDR to reach | optional — same |
| Flavor / Replicas | (runtime flavor / autoscaling as usual) | required, e.g. `general-2×4`, replicas for load |

Through the skills: `/agentbase-deploy` (runtime) and `/agentbase-gateway` (gateway) with network mode Private + VPC/Subnet/Route CIDRs. Record the choice in `.agentbase-state.json`.

## On-prem over VPN (pattern from the sample)

```
Agent (Runtime, Public or Private) → MCP Gateway (Private, Route CIDRs include on-prem CIDR)
  → your VPC (route: on-prem CIDR via the VPN gateway) → IPsec Site-to-Site → DC firewall → MCP server
```

- DC firewall: allow **only** the AgentBase/VPC source CIDRs to the MCP port. The sample uses the AgentBase VPC range observed for its org; confirm yours with GreenNode.
- The MCP server **still authenticates every call** (API key or JWT, fail-closed — `/agentbase-build-mcp-server`). A private network is not authentication.
- Connector outbound auth = API key / OAuth from Access Control (`/agentbase-build-identity`); the agent never holds it.

## Verify

1. VPC visible in the dropdown (otherwise peering is not active yet — refresh, then contact support).
2. From inside the VPC: the target answers (`curl https://<internal-host>/health`).
3. Gateway: `tools/list` through the connector returns the tools; `tools/call` is allowed by the Policy Group.
4. Timeouts only for private targets ⇒ missing **Route CIDRs** or a missing route/ACL on the VPC/DC side.
5. Name resolution fails for internal hosts ⇒ DNS resolution must be enabled in the VPC (MCP Gateway docs); prefer IPs or VPC DNS names that resolve inside the VPC.
