declare module '@amap/amap-jsapi-loader' {
  const Loader: { load(options: { key: string; version: string; plugins?: string[] }): Promise<any> }
  export default Loader
}
