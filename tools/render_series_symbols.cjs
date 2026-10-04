/* Render downloaded official vectors for Tk, preserving their paths and colors. */
const fs = require('node:fs');
const path = require('node:path');
const sharp = require(process.argv[2] || 'sharp');
const root = path.resolve(__dirname, '..', 'resources', 'series_symbols');
const brands = { '765': 'imas', million: 'imasml', cinderella: 'imascg',
  sidem: 'sidem', shiny: 'imassc', gakuen: 'gakuen' };

(async () => {
  const sources = [];
  for (const [key, brand] of Object.entries(brands)) {
    const original = fs.readFileSync(path.join(root, key + '.svg'));
    // The source uses 20–25% opacity as a large background decoration.
    // Keep the official artwork and color; remove only that display opacity.
    const visible = original.toString('utf8').replace(/opacity:\s*\.2(?:5)?;/g, 'opacity:1;');
    await sharp(Buffer.from(visible), { density: 144 }).resize(128, 128, {
      fit: 'contain', background: '#00000000'
    }).png().toFile(path.join(root, key + '.png'));
    sources.push({key, source_url: `https://idolmaster-official.jp/assets/img/about/${brand}/logo.svg`,
      original: key + '.svg', display: key + '.png'});
  }
  fs.writeFileSync(path.join(root, 'NOTICE.json'), JSON.stringify({
    source_page: 'https://idolmaster-official.jp/about',
    retrieved: '2026-10-03',
    rights: 'THE IDOLM@STER™ & ©Bandai Namco Entertainment Inc.',
    use: 'Official brand marks identifying catalog series; no affiliation implied.',
    processing: 'Original SVG paths and colors retained. Decorative 20–25% opacity removed for small UI display; rendered with transparent padding, preserving aspect ratio.',
    assets: sources
  }, null, 2) + '\n', 'utf8');
  console.log('Rendered ' + sources.length + ' official series marks.');
})();
