export interface AgentStep {
    stepId: number;
    type: 'thought' | 'action' | 'observation' | 'final' | 'error';
    content: string;
    tool?: string;
    toolInput?: string;
    timestamp: number;
    screenshot?: string; // Base64 encoded screenshot for browser view
}

export interface ChatMessage {
    id: string;
    role: 'user' | 'assistant';
    content: string;
    timestamp: number;
    turnId?: string; // 新增：用于匹配流式增量
    isHitl?: boolean;
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
