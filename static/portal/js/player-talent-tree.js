// 玩家详情与专精资料共用的天赋缩放及选择节点提示。
(function() {
    /* ===== Responsive Scaling ===== */
    function scaleTalentStage() {
        var wrappers = document.querySelectorAll('.talent-stage-scale-wrapper');
        wrappers.forEach(function(wrapper) {
            var stage = wrapper.querySelector('.talent-render-stage');
            if (!stage) return;
            var containerWidth = wrapper.parentElement.clientWidth;
            if (containerWidth <= 0) return;
            var stageWidth = parseInt(wrapper.getAttribute('data-stage-width')) || parseInt(stage.style.width);
            var stageHeight = parseInt(wrapper.getAttribute('data-stage-height')) || parseInt(stage.style.height);
            if (stageWidth > containerWidth) {
                var scale = containerWidth / stageWidth;
                stage.style.transform = 'scale(' + scale + ')';
                stage.style.transformOrigin = 'top left';
                wrapper.style.height = (stageHeight * scale) + 'px';
            } else {
                stage.style.transform = 'none';
                wrapper.style.height = stageHeight + 'px';
            }
        });
    }

    scaleTalentStage();
    window.addEventListener('resize', scaleTalentStage);
    if (window.ResizeObserver) {
        const observer = new ResizeObserver(scaleTalentStage);
        document.querySelectorAll('.talent-stage-scale-wrapper').forEach(wrapper => observer.observe(wrapper.parentElement));
    }

    /* ===== Choice Node Popup Hover ===== */
    var activeTooltip = null;

    document.querySelectorAll('.talent-node-card--tree.is-choice-node').forEach(function(node) {
        var popup = node.querySelector('.talent-choice-popup');
        if (!popup) return;
        var hoverTimeout = null;

        function showPopup() {
            if (hoverTimeout) clearTimeout(hoverTimeout);
            node.classList.add('popup-active');
            node.style.zIndex = '100';
            var nodeTooltip = node.querySelector('.talent-node-tooltip');
            if (nodeTooltip) nodeTooltip.style.display = 'none';
        }

        function hidePopup() {
            hoverTimeout = setTimeout(function() {
                node.classList.remove('popup-active');
                node.style.zIndex = '';
                hideOptionTooltip();
            }, 150);
        }

        node.addEventListener('mouseenter', showPopup);
        node.addEventListener('mouseleave', hidePopup);
        popup.addEventListener('mouseenter', showPopup);
        popup.addEventListener('mouseleave', hidePopup);

        /* Option tooltip on hover */
        popup.querySelectorAll('.talent-choice-popup-option').forEach(function(option) {
            option.addEventListener('mouseenter', function(e) {
                var name = option.getAttribute('data-option-name');
                var desc = option.getAttribute('data-option-desc');
                if (!name) return;
                showOptionTooltip(name, desc, option);
            });
            option.addEventListener('mouseleave', function() {
                hideOptionTooltip();
            });
        });
    });

    function showOptionTooltip(name, desc, anchor) {
        hideOptionTooltip();
        var tip = document.createElement('div');
        tip.className = 'talent-option-tooltip';
        const title = document.createElement('div');
        title.className = 'talent-option-tooltip-name';
        title.textContent = name;
        tip.append(title);
        if (desc) {
            const description = document.createElement('div');
            description.className = 'talent-option-tooltip-desc';
            description.textContent = desc;
            tip.append(description);
        }
        document.body.appendChild(tip);

        var rect = anchor.getBoundingClientRect();
        tip.style.position = 'fixed';
        tip.style.left = (rect.right + 10) + 'px';
        tip.style.top = rect.top + 'px';
        tip.style.zIndex = '99999';

        /* Adjust if off-screen */
        var tipRect = tip.getBoundingClientRect();
        if (tipRect.right > window.innerWidth - 10) {
            tip.style.left = (rect.left - tipRect.width - 10) + 'px';
        }
        if (tipRect.bottom > window.innerHeight - 10) {
            tip.style.top = (window.innerHeight - tipRect.height - 10) + 'px';
        }

        activeTooltip = tip;
    }

    function hideOptionTooltip() {
        if (activeTooltip) {
            activeTooltip.remove();
            activeTooltip = null;
        }
    }
})();
