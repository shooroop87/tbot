// postcss.config.js
const purgecssModule = require('@fullhuman/postcss-purgecss');
const cssnano = require('cssnano');

// Универсально достаём плагин (на случай .default)
const purgecss = purgecssModule.default || purgecssModule;

module.exports = {
  plugins: [
    purgecss({
      content: [
        'backend/templates/**/*.html',
        'backend/**/templates/**/*.html',
        'backend/**/*.py',
        'backend/**/static/**/*.js',
        '**/*.js'
      ],
      safelist: {
        standard: [
          'fa','fas','far','fab','fa-solid','fa-regular','fa-brands',
          'fa-fw','fa-li','fa-ul','fa-lg','fa-xs','fa-sm',
          'fa-1x','fa-2x','fa-3x','fa-4x','fa-5x','fa-6x','fa-7x','fa-8x','fa-9x','fa-10x',
          'fa-spin','fa-pulse','fa-rotate-90','fa-rotate-180','fa-rotate-270',
          'fa-flip-horizontal','fa-flip-vertical','fa-inverse',
          'fa-stack','fa-stack-1x','fa-stack-2x'
        ],
        // если генерируешь классы динамически, можешь временно подстраховаться:
        // greedy: [/^fa-[a-z0-9-]+$/]
      }
    }),
    cssnano({ preset: 'default' })
  ]
};
