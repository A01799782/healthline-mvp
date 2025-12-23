// Minimal RxNorm autocomplete for medication name inputs
(function () {
  const debounce = (fn, delay) => {
    let t;
    return (...args) => {
      clearTimeout(t);
      t = setTimeout(() => fn(...args), delay);
    };
  };

  function setupRxnorm(inputId, rxcuiId, rxnameId) {
    const input = document.getElementById(inputId);
    if (!input) return;
    const hiddenRxcui = document.getElementById(rxcuiId);
    const hiddenName = document.getElementById(rxnameId);

    let box = document.createElement("div");
    box.style.position = "absolute";
    box.style.background = "white";
    box.style.border = "1px solid #ccc";
    box.style.zIndex = 4000;
    box.style.display = "none";
    box.style.maxHeight = "180px";
    box.style.overflowY = "auto";
    box.style.minWidth = "200px";
    document.body.appendChild(box);

    const placeBox = () => {
      const rect = input.getBoundingClientRect();
      box.style.left = `${rect.left + window.scrollX}px`;
      box.style.top = `${rect.bottom + window.scrollY}px`;
      box.style.width = `${rect.width}px`;
    };

    const renderSuggestions = (items) => {
      box.innerHTML = "";
      if (!items || items.length === 0) {
        box.style.display = "none";
        input.dataset.rxnormOpen = "0";
        return;
      }
      input.dataset.rxnormOpen = "1";
      placeBox();
      items.forEach((item) => {
        const opt = document.createElement("div");
        opt.textContent = item.name;
        opt.style.padding = "6px";
        opt.style.cursor = "pointer";
        opt.addEventListener("mousedown", (e) => {
          e.preventDefault();
          input.value = item.name;
          if (hiddenRxcui) hiddenRxcui.value = item.rxcui || "";
          if (hiddenName) hiddenName.value = item.name || "";
          box.style.display = "none";
        });
        box.appendChild(opt);
      });
      box.style.display = "block";
    };

    const fetchSuggestions = debounce((q) => {
      if (!q || q.length < 3) {
        renderSuggestions([]);
        return;
      }
      fetch(`/api/rxnorm/suggest?${new URLSearchParams({ query: q })}`)
        .then((r) => r.json())
        .then((data) => renderSuggestions(data || []))
        .catch(() => renderSuggestions([]));
    }, 300);

    input.addEventListener("input", () => {
      if (hiddenRxcui) hiddenRxcui.value = "";
      if (hiddenName) hiddenName.value = "";
      fetchSuggestions(input.value.trim());
    });
    input.addEventListener("focus", placeBox);
    window.addEventListener("resize", placeBox);
    window.addEventListener("scroll", placeBox, true);

    document.addEventListener("click", (e) => {
      if (!box.contains(e.target) && e.target !== input) {
        box.style.display = "none";
      }
    });
  }

  window.setupRxnorm = setupRxnorm;
})();
