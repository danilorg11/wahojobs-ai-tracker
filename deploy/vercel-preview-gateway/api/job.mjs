import { createPreviewGatewayHandler } from '../lib/gateway.mjs';
import { publishedPathSet } from '../lib/release.mjs';

export default createPreviewGatewayHandler({
  routeClass: 'detail',
  ownsPath: (path) => publishedPathSet.has(path),
});
