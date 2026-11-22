// GitHub Pages uses the same release pointer as Nexus. This selects a manual
// download link; it is not a signature verifier, downloader or installer.
globalThis.CaelestiaDownloads = (() => {
    const repository = "KldsSeeGhosts/caelestia-windows";
    const github = `https://github.com/${repository}/releases`;
    const feed = `https://raw.githubusercontent.com/${repository}/nexus-channels/v1/development.dsse.json`;

    function fromEnvelope(envelope) {
        if (envelope?.payloadType !== "application/vnd.caelestia.update-channel+json"
            || typeof envelope.payload !== "string" || envelope.payload.length > 65536)
            return null;
        const pointer = JSON.parse(atob(envelope.payload));
        if (pointer?.type !== "caelestia-update-channel" || pointer.schema !== 1
            || pointer.repository !== repository || pointer.channel !== "development"
            || !(Date.parse(pointer.expires) > Date.now()))
            return null;
        const release = pointer.release;
        const version = release?.buildVersion;
        if (typeof version !== "string" || !/^(0|[1-9]\d{0,4})(\.(0|[1-9]\d{0,4})){2}$/.test(version)
            || /[\r\n]/.test(version) || version.split(".").some(part => Number(part) > 65535)
            || release.releaseId !== version || release.displayVersion !== version)
            return null;
        // Never use an unchecked link from a remote document. The sole
        // accepted destination is the exact version's Windows x64 asset.
        const tag = `v${version}-development`;
        const name = `caelestia-windows-${version}-windows-x64.zip`;
        const url = `${github}/download/${tag}/${name}`;
        const notes = `${github}/tag/${tag}`;
        const sha256 = release.bundle?.sha256;
        if (release.tag !== tag || release.bundle?.name !== name || release.bundle.url !== url
            || release.notesUrl !== notes || typeof sha256 !== "string" || sha256.length !== 64 || !/^[0-9a-f]+$/.test(sha256))
            return null;
        return {version, url, notes, sha256};
    }

    async function current() {
        try {
            const response = await fetch(feed, {cache: "no-cache", signal: AbortSignal.timeout(5000)});
            return response.ok ? fromEnvelope(await response.json()) : null;
        } catch {
            return null;
        }
    }

    return {current};
})();
