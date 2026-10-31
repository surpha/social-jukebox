// Build: npx tailwindcss@3 -c tailwind.config.js -i tailwind.input.css -o app/static/app.css --minify
module.exports = {
    content: ['./app/templates/**/*.html', './app/static/**/*.js'],
    theme: {
        extend: {
            colors: {
                'sp-green': '#1DB954',
                'sp-green-hover': '#1ed760',
                'sp-black': '#121212',
                'sp-dark': '#181818',
                'sp-card': '#282828',
                'sp-hover': '#333333',
                'sp-light': '#b3b3b3',
                'sp-white': '#ffffff',
            },
            fontFamily: { sans: ['Inter', 'system-ui', 'sans-serif'] },
        },
    },
};
