/* Agent collaboration state reducer and instance identity contract. */
'use strict';

// AGENT_COLLABORATION_STATE_CONTRACT_START
function createAgentCollaborationState() {
    return {
        agents: {}, messages: [], disputes: {}, activeDisputeId: '', handoffs: [],
        graph: { dependencies: [], invalidReason: '' },
        convergence: { status: 'running', terminationReason: '', iteration: 0 },
        parallel: { activeIds: [], peak: 0, overlap: false },
        resource: {
            state: 'idle', ramPeak: null, vramPeak: null, margin: null,
            estimatedPeak: null, actualPeak: null, calibrationFactor: 1,
            calibrationSamples: 0
        },
        externalModels: [], primaryModel: '', roleModels: {}, aliasOrdinals: {},
        aliasCounters: Object.fromEntries(Object.keys(AGENT_ROLE_META).map(role => [role, 0])),
        runId: '', step: 0, running: false
    };
}

function normalizeAgentRole(value) {
    const text = String(value || '').trim().toLowerCase();
    if (text.includes('explor') || text.includes('retrieve') || text.includes('research') || text.includes('inspect')) return 'explorer';
    if (text.includes('implement') || text.includes('coder') || text.includes('execute') || text.includes('fix')) return 'implementer';
    if (text.includes('critic') || text.includes('diagnos') || text.includes('review')) return 'critic';
    if (text.includes('verif') || text.includes('test') || text.includes('validat')) return 'verifier';
    return 'planner';
}

function collaborationAgentId(data = {}, role = 'planner', state = agentCollaborationState) {
    return String(data.agent_id || data.agentId || `${role}-${state.runId || 'pending'}`).trim();
}
function ensureCollaborationAgent(state, data = {}) {
    const role = normalizeAgentRole(data.role || data.name || data.agent_type || data.agent_id);
    const id = collaborationAgentId(data, role, state), current = state.agents[id] || {};
    const assignedBy = String(data.parent_agent_id || data.assigned_by || current.parentId || '').trim();
    const parentId = ['user', 'system', 'planner'].includes(assignedBy) ? '' : assignedBy;
    const incomingWorkerPid = Number(data.worker_pid ?? data.workerPid);
    const currentWorkerPid = Number(current.workerPid);
    const workerPid = Number.isSafeInteger(incomingWorkerPid) && incomingWorkerPid > 0
        ? incomingWorkerPid
        : Number.isSafeInteger(currentWorkerPid) && currentWorkerPid > 0
            ? currentWorkerPid
            : null;
    state.agents[id] = {
        id, role, realAgent: data.realAgent === true || current.realAgent === true,
        model: String(data.model || current.model || state.roleModels[role] || ''),
        task: String(data.task || data.objective || data.task_id || current.task || ''),
        taskId: String(data.task_id || current.taskId || ''), parentId,
        contextId: String(data.context_id || current.contextId || ''),
        runtimeId: String(data.runtime_id || current.runtimeId || ''),
        workerId: String(data.worker_id || current.workerId || ''),
        workerPid,
        status: String(data.status || current.status || 'queued'),
        startedAt: String(data.started_at || current.startedAt || ''),
        completedAt: String(data.completed_at || current.completedAt || ''),
        currentTool: String(data.currentTool || current.currentTool || ''),
        currentToolCallId: String(data.currentToolCallId || current.currentToolCallId || ''),
        toolCallCount: Number(current.toolCallCount || 0),
        modelRequestCount: Number(current.modelRequestCount || 0),
        lastModelRequestAt: String(data.lastModelRequestAt || current.lastModelRequestAt || ''),
        toolLoop: Number(data.tool_loop || data.toolLoop || current.toolLoop || 0),
        lastTool: String(data.lastTool || current.lastTool || ''),
        lastToolSuccess: data.lastToolSuccess ?? current.lastToolSuccess ?? null
    };
    return state.agents[id];
}
function isActiveCollaborationAgent(agent) {
    return !!agent?.realAgent && AGENT_ACTIVE_STATES.has(agent.status) && !AGENT_TERMINAL_STATES.has(agent.status);
}
function updateCollaborationParallel(state) {
    const active = Object.values(state.agents).filter(isActiveCollaborationAgent).map(agent => agent.id);
    state.parallel.activeIds = active; state.parallel.peak = Math.max(state.parallel.peak, active.length);
    state.parallel.overlap = state.parallel.overlap || active.length > 1;
}
function reduceAgentCollaborationState(state, eventType, data = {}) {
    const role = normalizeAgentRole(data.role || data.name || data.agent_type || data.agent_id);
    const timestamp = String(data.created_at || data.started_at || data.completed_at || '');
    if (eventType === 'meta') {
        state.runId = String(data.run_id || state.runId || '').trim();
        state.primaryModel = String(data.model || state.primaryModel || '').trim();
    }
    if (eventType === 'resource_guard' && Array.isArray(data.roles)) {
        data.roles.forEach(item => {
            const plannedRole = normalizeAgentRole(item.role);
            const plannedModel = String(item.model || '').trim();
            if (plannedModel) state.roleModels[plannedRole] = plannedModel;
        });
    }
    if (eventType === 'agent_spawned' || eventType === 'agent_worker_started') {
        const lifecycle = eventType === 'agent_worker_started'
            ? String(data.state || data.status || 'running')
            : String(data.status || 'queued');
        const agent = ensureCollaborationAgent(state, { ...data, status: lifecycle, realAgent: !!data.agent_id });
        if (!agent.startedAt && eventType === 'agent_worker_started') agent.startedAt = timestamp;
    }
    if (eventType === 'agent_status' || eventType === 'agent_execution_state') {
        const lifecycle = String(data.state || data.status || 'working');
        const agent = ensureCollaborationAgent(state, { ...data, realAgent: !!data.agent_id });
        const terminal = AGENT_TERMINAL_STATES.has(lifecycle);
        if (!AGENT_TERMINAL_STATES.has(agent.status) || terminal) agent.status = lifecycle;
        if (!agent.startedAt && AGENT_ACTIVE_STATES.has(agent.status)) agent.startedAt = timestamp;
        if (terminal) agent.completedAt = timestamp;
    }
    if (eventType === 'agent_completed' || eventType === 'agent_failed') {
        const agent = ensureCollaborationAgent(state, { ...data, realAgent: !!data.agent_id });
        agent.status = eventType === 'agent_completed' ? 'completed' : String(data.state || 'failed');
        agent.startedAt = agent.startedAt || timestamp; agent.completedAt = timestamp;
    }
    if (eventType === 'agent_model_request' && data.agent_id) {
        const agent = ensureCollaborationAgent(state, {
            ...data,
            status: undefined,
            realAgent: true
        });
        agent.modelRequestCount += 1;
        agent.lastModelRequestAt = timestamp;
    }
    if (['agent_message', 'planner_decision', 'critic_review'].includes(eventType) && data.agent_id) {
        ensureCollaborationAgent(state, { ...data, status: undefined, realAgent: true });
    }
    if (['tool_start', 'agent_tool_start'].includes(eventType) && data.agent_id) {
        const tool = String(data.tool || data.tool_name || 'tool');
        const agent = ensureCollaborationAgent(state, { ...data, status: undefined, realAgent: true });
        if (!AGENT_TERMINAL_STATES.has(agent.status)) agent.status = String(data.state || 'running');
        agent.currentTool = tool;
        agent.currentToolCallId = String(data.tool_call_id || data.call_id || `${agent.id}:${data.sequence || agent.toolCallCount + 1}`);
        agent.toolCallCount += 1;
        agent.toolLoop = Number(data.tool_loop || data.loop || data.iteration || agent.toolLoop || 0);
        agent.lastTool = tool;
        agent.lastToolSuccess = null;
    }
    if (['tool_end', 'agent_tool_end'].includes(eventType) && data.agent_id) {
        const tool = String(data.tool || data.tool_name || 'tool');
        const callId = String(data.tool_call_id || data.call_id || '');
        const success = data.success !== false && !String(data.result || '').toLowerCase().startsWith('error');
        const agent = ensureCollaborationAgent(state, { ...data, status: undefined, realAgent: true });
        if (!AGENT_TERMINAL_STATES.has(agent.status)) agent.status = success ? String(data.state || 'running') : 'challenged';
        if (!callId || callId === agent.currentToolCallId) {
            agent.currentTool = '';
            agent.currentToolCallId = '';
        }
        agent.lastTool = tool;
        agent.lastToolSuccess = success;
    }
    if (['handoff_created', 'agent_handoff', 'subagent_result'].includes(eventType)) {
        const producer = String(data.producer_agent_id || collaborationAgentId(data, role, state)), artifactId = String(data.artifact_id || `handoff-${producer}-${data.task_id || state.handoffs.length + 1}`);
        const consumers = data.consumer_agent_ids || (data.consumer_agent_id ? [data.consumer_agent_id] : []);
        if (!state.handoffs.some(item => item.artifactId === artifactId)) state.handoffs.push({
            artifactId, producerAgentId: producer, consumerAgentIds: consumers.map(String), sha256: String(data.sha256 || data.output_sha256 || ''),
            taskId: String(data.task_id || ''), summary: String(data.summary || data.message || ''), createdAt: timestamp,
            contractValid: data.contract_valid === true,
            evidenceCount: Number(data.evidence_count || 0),
            consumedArtifactIds: Array.isArray(data.consumed_artifact_ids) ? data.consumed_artifact_ids.map(String) : [],
            superseded: false
        });
    }
    if (eventType === 'collaboration_scheduling') {
        state.graph.dependencies = Array.isArray(data.dependency_edges)
            ? data.dependency_edges.map(edge => ({
                producerAgentId: String(edge.producer_agent_id || ''),
                consumerAgentId: String(edge.consumer_agent_id || ''),
                ready: false, artifactId: ''
            })).filter(edge => edge.producerAgentId && edge.consumerAgentId)
            : [];
    }
    if (eventType === 'collaboration_dependency_ready') {
        const producer = String(data.logical_producer_agent_id || data.producer_agent_id || '');
        const consumer = String(data.consumer_agent_id || '');
        const edge = state.graph.dependencies.find(item => item.producerAgentId === producer && item.consumerAgentId === consumer);
        if (edge) { edge.ready = true; edge.artifactId = String(data.artifact_id || ''); }
    }
    if (eventType === 'collaboration_graph_invalid') state.graph.invalidReason = String(data.error || 'invalid dependency graph');
    if (eventType === 'handoff_superseded') {
        const oldArtifact = state.handoffs.find(item => item.artifactId === String(data.old_artifact_id || ''));
        if (oldArtifact) oldArtifact.superseded = true;
    }
    if (eventType === 'critic_review' && Array.isArray(data.disputes) && data.disputes.length) data.disputes.forEach((item, index) => {
        const id = String(item.dispute_id || data.dispute_id || `dispute-${data.agent_id || 'critic'}-${data.task_id || index}`);
        state.disputes[id] = { id, detail: String(item.issue || data.summary || ''), resolved: false, resolution: '' };
        state.activeDisputeId = id;
    });
    if (eventType === 'repair') {
        const id = String(data.dispute_id || `repair-${data.round || Object.keys(state.disputes).length + 1}`);
        state.disputes[id] ||= { id, detail: String(data.reason || data.message || ''), resolved: false, resolution: '' };
        state.activeDisputeId = id;
    }
    if (eventType === 'dispute_resolved' || eventType === 'critic_re_review' || (eventType === 'critic_review' && data.re_review_of)) {
        const id = String(data.dispute_id || data.re_review_of || '');
        const accepted = eventType === 'dispute_resolved' ? data.status !== 'rejected' && data.resolved !== false : data.resolved === true || ['accept', 'accepted', 'resolved'].includes(String(data.verdict || data.status));
        if (id && state.disputes[id] && accepted) {
            state.disputes[id].resolved = true; state.disputes[id].resolution = String(data.resolution || data.summary || '已完成同一爭議的再審');
        }
    }
    if (eventType === 'collaboration_convergence') state.convergence = {
        status: String(data.status || (data.termination_reason === 'accepted' ? 'accepted' : 'terminated')),
        terminationReason: String(data.termination_reason || ''), iteration: Number(data.iteration || data.rounds || data.round || 0)
    };
    if (eventType === 'deadline_exceeded') state.convergence = { status: 'terminated', terminationReason: 'deadline', iteration: state.convergence.iteration };
    if (eventType === 'cancelled') state.convergence = { status: 'terminated', terminationReason: 'cancelled', iteration: state.convergence.iteration };
    updateCollaborationParallel(state); return state;
}
if (typeof window !== 'undefined') window.WorkbenchAgentCollaborationContract = {
    createState: createAgentCollaborationState, reduceEvent: reduceAgentCollaborationState, ensureAgent: ensureCollaborationAgent
};
// AGENT_COLLABORATION_STATE_CONTRACT_END
