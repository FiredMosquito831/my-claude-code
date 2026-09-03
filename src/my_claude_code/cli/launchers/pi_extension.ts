import type { ExtensionAPI, ProviderModelConfig } from "@earendil-works/pi-coding-agent";

// MCC's own attribution header and the harness id it carries. Restated here
// rather than imported because this file is TypeScript and the definitions are
// Python -- `core/client_fingerprint.HARNESS_HEADER` and the `pi` entry in
// `config/harnesses.HARNESS_SPECS`. `tests/cli/test_entrypoints.py` reads this
// source and asserts both halves still match those two, which is the closest
// thing to a shared constant a language boundary allows.
const HARNESS_HEADER = "x-mcc-harness";
const HARNESS_ID = "pi";
const API_KEY_ENV = "FCC_PI_API_KEY";
const BASE_URL_ENV = "FCC_PI_BASE_URL";
const CATALOG_TIMEOUT_MS = 3000;
const CATALOGUE_MODELS_PATH = "/admin/api/catalogue-models";
// Only reached when the proxy is older than this extension and has no
// capability-bearing route. Every number below is then unknown, so the models
// carry Pi's own defaults; the capability route is what makes them real.
const FALLBACK_CONTEXT_WINDOW = 128000;
const FALLBACK_MAX_TOKENS = 16384;
const NORMAL_MODEL_PREFIX = "anthropic/";
const NO_THINKING_MODEL_PREFIX = "claude-3-freecc-no-thinking/";

function requireEnvironment(name: string): string {
	const value = process.env[name]?.trim();
	if (!value) {
		throw new Error(`Missing required ${name} environment variable.`);
	}
	return value;
}

function normalizeBaseUrl(value: string): string {
	let url: URL;
	try {
		url = new URL(value);
	} catch {
		throw new Error(`${BASE_URL_ENV} is not a valid URL.`);
	}
	if (url.protocol !== "http:" && url.protocol !== "https:") {
		throw new Error(`${BASE_URL_ENV} must use http or https.`);
	}
	url.search = "";
	url.hash = "";
	return url.toString().replace(/\/+$/, "");
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function catalogModelIds(payload: unknown): string[] {
	if (!isRecord(payload) || payload.object !== "list" || !Array.isArray(payload.data)) {
		throw new Error("MCC model catalog returned an invalid response shape.");
	}

	const ids: string[] = [];
	for (const entry of payload.data) {
		if (!isRecord(entry) || typeof entry.id !== "string") continue;
		const id = entry.id.trim();
		if (id) ids.push(id);
	}
	return ids;
}

function providerModelRef(id: string, prefix: string): string | undefined {
	if (!id.startsWith(prefix)) return undefined;
	const parts = id.slice(prefix.length).split("/");
	if (parts.length < 2 || parts.some((part) => !part)) return undefined;
	return parts.join("/");
}

function modelDefinition(providerModel: string, reasoning: boolean): ProviderModelConfig {
	return {
		id: providerModel,
		name: providerModel,
		reasoning,
		input: ["text"],
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
		contextWindow: FALLBACK_CONTEXT_WINDOW,
		maxTokens: FALLBACK_MAX_TOKENS,
	};
}

export function projectFccModels(payload: unknown): ProviderModelConfig[] {
	const ids = catalogModelIds(payload);
	const normalModels = new Set<string>();
	for (const id of ids) {
		const providerModel = providerModelRef(id, NORMAL_MODEL_PREFIX);
		if (providerModel) normalModels.add(providerModel);
	}

	const models: ProviderModelConfig[] = [];
	const seen = new Set<string>();
	for (const id of ids) {
		const normalModel = providerModelRef(id, NORMAL_MODEL_PREFIX);
		if (normalModel) {
			if (!seen.has(normalModel)) {
				seen.add(normalModel);
				models.push(modelDefinition(normalModel, true));
			}
			continue;
		}

		const noThinkingModel = providerModelRef(id, NO_THINKING_MODEL_PREFIX);
		if (!noThinkingModel || normalModels.has(noThinkingModel) || seen.has(noThinkingModel)) continue;
		seen.add(noThinkingModel);
		models.push(modelDefinition(noThinkingModel, false));
	}

	if (models.length === 0) {
		throw new Error("MCC model catalog contains no routable provider models.");
	}
	return models;
}

function isModelConfig(value: unknown): value is ProviderModelConfig {
	return isRecord(value) && typeof value.id === "string" && value.id.trim() !== "";
}

/**
 * Project MCC's capability-bearing catalogue payload into Pi's model list.
 *
 * The server has already run Pi's own serialiser over the resolution ladder,
 * so every contextWindow, maxTokens, reasoning flag and cost here is either a
 * value some provider actually published or a Pi default the server recorded
 * under `_mcc_defaulted`. Nothing is re-derived on this side: a second copy of
 * the mapping is a second thing to drift.
 */
export function projectMccCatalogueModels(payload: unknown): ProviderModelConfig[] {
	if (!isRecord(payload) || !isRecord(payload.catalogues)) {
		throw new Error("MCC catalogue route returned an invalid response shape.");
	}
	const entry = payload.catalogues.pi;
	if (!isRecord(entry) || !isRecord(entry.document) || !Array.isArray(entry.document.models)) {
		throw new Error("MCC catalogue route carried no Pi catalogue.");
	}
	const models = entry.document.models.filter(isModelConfig);
	if (models.length === 0) {
		throw new Error("MCC catalogue contains no routable provider models.");
	}
	return models;
}

function requestIdSuffix(response: Response): string {
	const requestId = response.headers.get("request-id") ?? response.headers.get("x-request-id");
	return requestId ? ` (request ${requestId})` : "";
}

async function fetchJson(baseUrl: string, path: string, apiKey: string): Promise<unknown> {
	const controller = new AbortController();
	const timeout = setTimeout(() => controller.abort(), CATALOG_TIMEOUT_MS);
	try {
		let response: Response;
		try {
			response = await fetch(`${baseUrl}${path}`, {
				headers: { Authorization: `Bearer ${apiKey}` },
				signal: controller.signal,
			});
		} catch (error) {
			if (error instanceof Error && error.name === "AbortError") {
				throw new Error(`MCC model catalog timed out after ${CATALOG_TIMEOUT_MS}ms.`);
			}
			const message = error instanceof Error ? error.message : String(error);
			throw new Error(`Could not reach the MCC model catalog: ${message}`);
		}

		if (!response.ok) {
			throw new Error(`MCC model catalog returned HTTP ${response.status}${requestIdSuffix(response)}.`);
		}

		try {
			return await response.json();
		} catch (error) {
			if (error instanceof Error && error.name === "AbortError") {
				throw new Error(`MCC model catalog timed out after ${CATALOG_TIMEOUT_MS}ms.`);
			}
			throw new Error(`MCC model catalog returned invalid JSON${requestIdSuffix(response)}.`);
		}
	} finally {
		clearTimeout(timeout);
	}
}

async function fetchFccModels(baseUrl: string, apiKey: string): Promise<ProviderModelConfig[]> {
	try {
		return projectMccCatalogueModels(await fetchJson(baseUrl, CATALOGUE_MODELS_PATH, apiKey));
	} catch (error) {
		// An MCC old enough to lack the capability route still has /v1/models.
		// Falling back keeps the session working; it costs the real numbers,
		// which is exactly what upgrading the proxy restores.
		const message = error instanceof Error ? error.message : String(error);
		console.warn(`My Claude Code: falling back to /v1/models without capabilities (${message}).`);
		return projectFccModels(await fetchJson(baseUrl, "/v1/models", apiKey));
	}
}

export default async function freeClaudeCode(pi: ExtensionAPI): Promise<void> {
	const baseUrl = normalizeBaseUrl(requireEnvironment(BASE_URL_ENV));
	const apiKey = requireEnvironment(API_KEY_ENV);
	const models = await fetchFccModels(baseUrl, apiKey);

	pi.registerProvider("free-claude-code", {
		name: "My Claude Code",
		baseUrl,
		apiKey: `$${API_KEY_ENV}`,
		authHeader: true,
		api: "anthropic-messages",
		// Pi's ProviderConfig takes `headers?: Record<string, string>` and
		// merges it into every request the provider makes. The one header MCC
		// adds is not a credential -- `apiKey` above is the credential, and it
		// is a reference to the environment, not a literal -- it is the label
		// that lets the request log say "Pi sent this" rather than inferring it
		// from a user-agent.
		headers: { [HARNESS_HEADER]: HARNESS_ID },
		models,
	});
}
