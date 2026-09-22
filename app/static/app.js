/* 前端逻辑：Vue 3（CDN）+ fetch 调 REST 接口（ES6+：const/let、模板字符串、async/await、解构） */
const { createApp } = Vue;

async function getJSON(url) {
  const resp = await fetch(url, { headers: { Accept: 'application/json' } });
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(`${resp.status} ${body.error || resp.statusText}`);
  }
  return resp.json();
}

createApp({
  data() {
    return {
      items: [], total: 0, page: 1, size: 20,
      status: '', line: '', orderBy: 'id', desc: false,
      statuses: ['running', 'idle', 'fault', 'maintenance'],
      lines: ['A1', 'A2', 'B1', 'B2'],
      stats: { devices: 0, avg_temperature: 0, max_temperature: 0, fault_ratio: 0 },
      cache: { hits: 0, misses: 0, hit_rate: 0 },
      error: '',
    };
  },
  computed: {
    totalPages() {
      return Math.max(1, Math.ceil(this.total / this.size));
    },
  },
  methods: {
    async reload() {
      this.error = '';
      try {
        const q = new URLSearchParams({
          page: String(this.page), size: String(this.size),
          order_by: this.orderBy, desc: this.desc ? '1' : '0',
        });
        if (this.status) q.set('status', this.status);
        if (this.line) q.set('line', this.line);

        const [list, stats, cache] = await Promise.all([
          getJSON(`/api/devices?${q.toString()}`),
          getJSON('/api/stats'),
          getJSON('/api/cache/stats'),
        ]);
        this.items = list.items;
        this.total = list.total;
        this.stats = stats;
        this.cache = cache;
        document.title = `设备数据看板 · ${this.total} 台`;
      } catch (err) {
        this.error = `加载失败：${err.message}`;
      }
    },
    go(p) {
      this.page = Math.min(Math.max(1, p), this.totalPages);
      this.reload();
    },
  },
  mounted() {
    this.reload();
  },
}).mount('#app');
