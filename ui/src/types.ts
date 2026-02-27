export interface AgentStep {
    stepId?: number;
    type: 'thought' | 'action' | 'observation' | 'final' | 'error';
    content: string;
    tool?: string;
    toolInput?: unknown;
    actions?: Array<Record<string, unknown>>;
    observations?: Array<Record<string, unknown>>;
    timestamp: number;
}

export interface ChatMessage {
    id: string;
    role: 'user' | 'assistant';
    content: string;
    timestamp: number;
    msg_id?: string;
    requestId?: string; // HITL confirm 唯一标识
    isHitl?: boolean;
    hitlType?: 'confirm' | 'request';
}

export interface Message {
    role: 'user' | 'model' | 'system';
    parts: { text: string }[];
}

export interface Tool {
    name: string;
    description: string;
    execute: (input: string) => Promise<string>;
}

export const PYTHON_REFERENCE_CODE = `# Removed in favor of Browser View implementation`;
