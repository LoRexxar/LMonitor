(function () {
    'use strict';

    const SVG_NS = 'http://www.w3.org/2000/svg';
    let instanceSequence = 0;

    function svgElement(name, attributes = {}) {
        const element = document.createElementNS(SVG_NS, name);
        for (const [key, value] of Object.entries(attributes)) {
            if (value !== undefined && value !== null && value !== '') {
                element.setAttribute(key, String(value));
            }
        }
        return element;
    }

    function finiteNumber(value, fallback) {
        const number = Number(value);
        return Number.isFinite(number) ? number : fallback;
    }

    function boundedNumber(value, fallback, minimum, maximum) {
        return Math.min(maximum, Math.max(minimum, finiteNumber(value, fallback)));
    }

    function normalizeBackground(value) {
        const color = String(value || '').replace(/^#/, '').trim();
        return /^[0-9a-f]{6}$/i.test(color) ? `#${color}` : '#160f0b';
    }

    function nodeIdentity(node) {
        return String(node.node_key || `${node.tree_type || 'spec'}:${node.node_id || node.talent_id || node.spell_id || ''}`);
    }

    function selected(node) {
        return Number(node.points || 0) > 0 || node.selected === true;
    }

    /**
     * Reusable talent topology renderer.
     *
     * SimC result pages can load this asset and call:
     * TalentTreeThumbnail.mount(element, {buildCode}, {width: 150});
     * Call render(renderModel, context) instead when the API payload is already available.
     */
    class TalentTreeThumbnail {
        constructor(container, options = {}) {
            if (typeof container === 'string') container = document.querySelector(container);
            if (!container) throw new Error('TalentTreeThumbnail requires a container');
            this.container = container;
            this.originalStyle = container.getAttribute('style');
            this.options = {
                endpoint: options.endpoint || '/portal/api/talents/simulator/',
                width: boundedNumber(options.width, 320, 80, 1600),
                padding: boundedNumber(options.padding, 18, 0, 120),
                background: normalizeBackground(options.background || options.bgcolor),
                borderRadius: boundedNumber(options.borderRadius, 0, 0, 80),
            };
            this.instanceId = `talent-thumbnail-${++instanceSequence}`;
            this.abortController = null;
            this.payload = null;
            this.svg = null;
        }

        static async mount(container, params = {}, options = {}) {
            const renderer = new TalentTreeThumbnail(container, options);
            await renderer.load(params);
            return renderer;
        }

        static buildRequestUrl(params = {}, endpoint = '/portal/api/talents/simulator/') {
            const query = new URLSearchParams();
            const values = {
                class: params.className || params.class || '',
                spec: params.specName || params.spec || '',
                version: params.versionKey || params.version || '',
                code: params.buildCode || params.code || '',
                hero: params.heroSubtree || params.hero || '',
                profile_id: params.profileId || params.profile_id || params.profile || '',
            };
            for (const [key, value] of Object.entries(values)) {
                if (value !== undefined && value !== null && String(value) !== '') {
                    query.set(key, String(value));
                }
            }
            return `${endpoint}${endpoint.includes('?') ? '&' : '?'}${query.toString()}`;
        }

        async load(params = {}) {
            if (this.abortController) this.abortController.abort();
            const controller = new AbortController();
            this.abortController = controller;
            this.showMessage('正在加载天赋缩略图…');
            try {
                const response = await fetch(
                    TalentTreeThumbnail.buildRequestUrl(params, this.options.endpoint),
                    {signal: controller.signal},
                );
                let payload;
                try {
                    payload = await response.json();
                } catch (error) {
                    throw new Error(response.ok ? '天赋缩略图接口返回格式错误' : `天赋缩略图加载失败（HTTP ${response.status}）`);
                }
                if (!response.ok || !payload.success) {
                    throw new Error(payload.error || '天赋缩略图加载失败');
                }
                if (controller.signal.aborted) return payload;
                this.payload = payload;
                this.render(payload.render_model || {}, payload);
                return payload;
            } catch (error) {
                if (error.name !== 'AbortError') this.showMessage(error.message || '天赋缩略图加载失败', true);
                throw error;
            } finally {
                if (this.abortController === controller) this.abortController = null;
            }
        }

        render(renderModel, context = {}) {
            const trees = (renderModel?.trees || []).filter(tree => tree.tree_type !== 'build_code');
            const nodes = trees.flatMap(tree => tree.nodes || []);
            if (!nodes.length) {
                this.showMessage('暂无可展示的天赋树');
                return null;
            }

            const padding = this.options.padding;
            const bounds = nodes.reduce((box, node) => {
                const x = finiteNumber(node.x, 0);
                const y = finiteNumber(node.y, 0);
                const width = Math.max(20, finiteNumber(node.width, 36));
                const height = Math.max(20, finiteNumber(node.height, 36));
                box.minX = Math.min(box.minX, x);
                box.minY = Math.min(box.minY, y);
                box.maxX = Math.max(box.maxX, x + width);
                box.maxY = Math.max(box.maxY, y + height);
                return box;
            }, {minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity});
            const viewWidth = Math.max(1, bounds.maxX - bounds.minX + padding * 2);
            const viewHeight = Math.max(1, bounds.maxY - bounds.minY + padding * 2);
            const offsetX = padding - bounds.minX;
            const offsetY = padding - bounds.minY;
            const nodeIndex = new Map(nodes.map(node => [nodeIdentity(node), node]));

            const svg = svgElement('svg', {
                class: 'talent-tree-thumbnail-svg',
                viewBox: `0 0 ${viewWidth} ${viewHeight}`,
                role: 'img',
                'aria-label': context.spec_cn ? `${context.spec_cn}天赋缩略图` : '天赋缩略图',
                preserveAspectRatio: 'xMidYMid meet',
            });
            svg.style.display = 'block';
            svg.style.width = '100%';
            svg.style.height = 'auto';
            svg.style.background = this.options.background;
            svg.style.borderRadius = `${this.options.borderRadius}px`;

            const graph = svgElement('g', {transform: `translate(${offsetX} ${offsetY})`});
            const pathLayer = svgElement('g', {class: 'talent-tree-thumbnail-paths'});
            for (const tree of trees) {
                for (const path of tree.paths || []) {
                    const parentSelected = selected(nodeIndex.get(String(path.parent_key)) || {});
                    const childSelected = selected(nodeIndex.get(String(path.child_key)) || {});
                    const active = parentSelected && childSelected;
                    const pathElement = svgElement('path', {
                        d: path.svg_path || '',
                        fill: 'none',
                        stroke: active ? '#f4b942' : '#34383f',
                        'stroke-width': active ? 5 : 4,
                        'stroke-linecap': 'round',
                        'stroke-linejoin': 'round',
                        opacity: active ? 1 : 0.9,
                    });
                    pathLayer.appendChild(pathElement);
                }
            }
            graph.appendChild(pathLayer);

            const nodeLayer = svgElement('g', {class: 'talent-tree-thumbnail-nodes'});
            for (const node of nodes) {
                const x = finiteNumber(node.x, 0);
                const y = finiteNumber(node.y, 0);
                const width = Math.max(20, finiteNumber(node.width, 36));
                const height = Math.max(20, finiteNumber(node.height, 36));
                const size = Math.min(width, height);
                const cx = x + width / 2;
                const cy = y + height / 2;
                const isSelected = selected(node);
                const group = svgElement('g', {
                    class: `talent-tree-thumbnail-node ${isSelected ? 'is-selected' : 'is-unselected'}`,
                });
                const title = svgElement('title');
                title.textContent = String(node.display_name || node.name || '未命名天赋');
                group.appendChild(title);
                group.appendChild(svgElement('circle', {
                    cx, cy, r: Math.max(7, size / 2),
                    fill: isSelected ? '#f4b942' : '#34383f',
                }));
                nodeLayer.appendChild(group);
            }
            graph.appendChild(nodeLayer);
            svg.appendChild(graph);

            this.container.replaceChildren(svg);
            this.container.classList.add('talent-tree-thumbnail');
            this.container.style.width = `${this.options.width}px`;
            this.container.style.maxWidth = '100%';
            this.container.style.lineHeight = '0';
            this.svg = svg;
            return svg;
        }

        showMessage(message, isError = false) {
            const element = document.createElement('div');
            element.className = `talent-tree-thumbnail-message${isError ? ' is-error' : ''}`;
            element.textContent = message;
            element.style.boxSizing = 'border-box';
            element.style.width = `${this.options.width}px`;
            element.style.maxWidth = '100%';
            element.style.minHeight = '96px';
            element.style.display = 'flex';
            element.style.alignItems = 'center';
            element.style.justifyContent = 'center';
            element.style.padding = '12px';
            element.style.borderRadius = `${this.options.borderRadius}px`;
            element.style.background = this.options.background;
            element.style.color = isError ? '#fca5a5' : '#cbd5e1';
            element.style.font = '600 12px/1.5 system-ui, sans-serif';
            element.style.textAlign = 'center';
            this.container.replaceChildren(element);
            this.svg = null;
        }

        destroy() {
            if (this.abortController) this.abortController.abort();
            this.abortController = null;
            this.payload = null;
            this.svg = null;
            this.container.replaceChildren();
            this.container.classList.remove('talent-tree-thumbnail');
            if (this.originalStyle === null) this.container.removeAttribute('style');
            else this.container.setAttribute('style', this.originalStyle);
        }
    }

    window.TalentTreeThumbnail = TalentTreeThumbnail;

    async function mountAutomaticThumbnail() {
        const container = document.querySelector('[data-talent-thumbnail-auto]');
        if (!container) return;
        const query = new URLSearchParams(window.location.search);
        const options = {
            endpoint: container.dataset.endpoint || '/portal/api/talents/simulator/',
            width: query.get('width') || container.dataset.width || 320,
            background: query.get('bgcolor') || container.dataset.background || '160f0b',
        };
        const params = {
            className: container.dataset.class || '',
            specName: container.dataset.spec || '',
            versionKey: query.get('version') || container.dataset.version || '',
            buildCode: query.get('code') || '',
            heroSubtree: query.get('hero') || '',
            profileId: query.get('profile_id') || query.get('profile') || '',
        };
        try {
            container.__talentTreeThumbnail = await TalentTreeThumbnail.mount(container, params, options);
        } catch (error) {
            if (error.name !== 'AbortError') console.error(error);
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', mountAutomaticThumbnail, {once: true});
    } else {
        mountAutomaticThumbnail();
    }
})();
