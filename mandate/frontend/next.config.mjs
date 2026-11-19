/** @type {import('next').NextConfig} */
const vercelEnv = process.env.VERCEL_ENV;
if ((vercelEnv === "production" || vercelEnv === "preview") && process.env.NEXT_PUBLIC_MANDATE_OWNER_TOKEN) {
  throw new Error("NEXT_PUBLIC_MANDATE_OWNER_TOKEN must not be set for a Vercel production or preview build");
}

const deployed = vercelEnv === "production" || vercelEnv === "preview";
const nextConfig = {
  reactStrictMode: true,
  env: {
    NEXT_PUBLIC_MANDATE_PROXY: deployed ? "1" : "",
  },
};

export default nextConfig;
