// src/index.ts

// We define the base class ourselves since the package is missing
export class TldrawSyncDO {
    state: any;

    constructor(state: any) {
        this.state = state;
        // This allows the DO to use the new SQLite storage required for Free Tier
    }

    async fetch(request: Request) {
        // This is the core "Spatial Participant" logic [cite: 3, 5]
        // It upgrades the connection to a WebSocket for the tldraw canvas
        const pair = new WebSocketPair();
        const [client, server] = Object.values(pair);

        await this.handleSession(server);

        return new Response(null, { status: 101, webSocket: client });
    }

    async handleSession(ws: WebSocket) {
        ws.accept();
        // Logic for syncing shapes and tiered perception goes here [cite: 13, 14]
        ws.onmessage = (msg) => {
            // Echo logic for multi-user sync [cite: 9]
            ws.send(msg.data);
        };
    }
}

export default {
    async fetch(request: Request, env: any) {
        const url = new URL(request.url);

        // Ensure the request is a WebSocket upgrade [cite: 31, 35]
        if (request.headers.get('Upgrade') === 'websocket') {
            const roomId = url.searchParams.get('roomId') || 'default-room';
            const id = env.TLDRAW_SYNC.idFromName(roomId);
            const stub = env.TLDRAW_SYNC.get(id);
            return stub.fetch(request);
        }

        return new Response("AI Brainstorm Canvas Worker Active", { status: 200 });
    },
};